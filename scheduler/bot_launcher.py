"""Launches one bot container per due meeting.

Docker-outside-of-Docker: this runs inside the `scheduler` container but
launches SIBLING containers on the host via the mounted docker.sock. Bind
mount paths therefore have to be given in HOST terms (HOST_PROJECT_DIR), not
this container's own paths — this container and the bot container it starts
are both children of the same host Docker daemon, not nested.
"""
import time
from datetime import datetime, timedelta, timezone

import docker
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from common.config import Settings, get_settings
from common.db import SessionLocal
from common.logging import get_logger
from common.models import Meeting, MeetingStatus

logger = get_logger(__name__)

# A launch failure reverts the meeting to `scheduled` so the next poll
# retries, rather than leaving it stuck `joining` with no container ever
# started. Bounded so a persistently broken launch (bad image, Docker
# daemon down, a misconfigured env var) doesn't retry forever — after this
# many failed attempts the meeting is marked `failed` instead.
MAX_LAUNCH_ATTEMPTS = 3


def claim_meeting(db: Session, meeting_id) -> bool:
    """Atomically flips scheduled -> joining so two launcher ticks (or
    replicas) can never launch two containers for the same meeting. The bot
    container itself also sets this status on start, but claiming it here
    first is what prevents a double-launch race in the launcher loop."""
    result = db.execute(
        text("UPDATE meetings SET status = 'joining' WHERE id = :id AND status = 'scheduled'"),
        {"id": str(meeting_id)},
    )
    db.commit()
    return result.rowcount == 1


def due_meetings(db: Session, lead_seconds: int) -> list[Meeting]:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    deadline = now + timedelta(seconds=lead_seconds)
    # Meetings far enough in the past are presumably a missed/stuck launch
    # (launcher was down, etc.) — skip rather than launch a bot hours late.
    grace_cutoff = now - timedelta(minutes=10)
    stmt = select(Meeting).where(
        Meeting.status == MeetingStatus.SCHEDULED,
        Meeting.scheduled_at <= deadline,
        Meeting.scheduled_at >= grace_cutoff,
    )
    return list(db.scalars(stmt))


def _bot_environment(settings: Settings, meeting_id) -> dict:
    """Passes every setting through as an env var (field_name -> FIELD_NAME),
    not a hand-picked subset. `docker compose run bot` gets the whole .env
    file via env_file: — containers launched here have no .env to read at
    all (no bind-mounted repo, no env_file:), so hand-picking a handful of
    keys would mean every new bot-behavior setting (BOT_JOIN_MODE, timeouts,
    etc.) silently falls back to its hardcoded default for auto-launched
    bots while manually-run ones pick up .env correctly — a real behavior
    split between the two launch paths neither of us would notice until a
    bot in production behaved differently from local testing."""
    env = {k.upper(): str(v) for k, v in settings.model_dump().items() if v is not None}
    env["MEETING_ID"] = str(meeting_id)
    env["BOT_MODE"] = "run"
    return env


def launch_container(settings: Settings, meeting_id) -> None:
    """Uses the low-level API (create_host_config + create_container +
    start), not the containers.run() convenience wrapper. run() silently
    rejects stop_timeout — confirmed live: `TypeError: run() got an
    unexpected keyword argument 'stop_timeout'` — because docker-py's run()
    only forwards kwargs listed in its own RUN_CREATE_KWARGS/
    RUN_HOST_CONFIG_KWARGS allowlists, and stop_timeout is in neither
    (checked against the installed docker-py directly). The low-level
    create_container() genuinely supports stop_timeout (it maps straight to
    the container config's StopTimeout) — that's the whole point: the grace
    period before SIGKILL on a future `docker stop` of this container,
    mirroring stop_grace_period on the `bot` service in docker-compose.yml.
    shm_size/network/auto-remove/volumes are reconstructed by hand below
    the same way containers.run() does it internally (docker-py's own
    _create_container_args), just without relying on that private helper.

    Raises on any failure (including a missing HOST_PROJECT_DIR, previously
    just logged-and-returned) — callers must react, since a thrown
    exception here means no container exists at all. This used to swallow
    docker.errors.APIError internally and only log it, which combined with
    claim_meeting's status flip is exactly how a meeting got stuck
    `joining` forever with no bot ever running: the exception (a TypeError,
    which wasn't even caught by that except clause) propagated past the
    whole per-tick loop with nothing to revert the status."""
    if not settings.host_project_dir:
        raise RuntimeError("HOST_PROJECT_DIR is not set — cannot bind-mount volumes for an auto-launched bot")

    client = docker.from_env()
    container_name = f"meet-bot-{meeting_id}"
    volumes = {
        f"{settings.host_project_dir}/bot-profiles": {"bind": "/bot-profiles", "mode": "rw"},
        f"{settings.host_project_dir}/recordings": {"bind": "/recordings", "mode": "rw"},
    }
    host_config = client.api.create_host_config(
        network_mode=settings.docker_network,
        # Docker's default /dev/shm (64MB) is too small for Chromium and
        # causes "Aw, Snap!" tab crashes — mirrors the `bot` service's
        # shm_size in docker-compose.yml for manually-run bots.
        shm_size="2g",
        binds=volumes,
        auto_remove=True,
    )
    container = client.api.create_container(
        image=settings.bot_image,
        name=container_name,
        detach=True,
        environment=_bot_environment(settings, meeting_id),
        volumes=[v["bind"] for v in volumes.values()],
        host_config=host_config,
        stop_timeout=settings.bot_stop_timeout_seconds,
    )
    client.api.start(container=container["Id"])
    logger.info("bot_container_launched", meeting_id=str(meeting_id), container=container_name)


def _handle_launch_failure(db: Session, meeting: Meeting, error: str) -> None:
    """Reverts to `scheduled` (so the next due_meetings() poll retries) for
    up to MAX_LAUNCH_ATTEMPTS, then gives up and marks `failed`. `status`
    is the externally-visible alert signal here (API, dashboard, outbound
    webhook in Phase 6) — same convention as
    checkout_or_mark_unavailable's NO_BOT_AVAILABLE in bot_pool/service.py;
    there's no outbound notification channel built yet."""
    meeting.launch_attempts += 1
    meeting.error = error
    if meeting.launch_attempts < MAX_LAUNCH_ATTEMPTS:
        meeting.status = MeetingStatus.SCHEDULED
        logger.warning(
            "bot_launch_failed_will_retry",
            meeting_id=str(meeting.id),
            attempt=meeting.launch_attempts,
            max_attempts=MAX_LAUNCH_ATTEMPTS,
            error=error,
        )
    else:
        meeting.status = MeetingStatus.FAILED
        logger.error(
            "bot_launch_failed_permanently",
            meeting_id=str(meeting.id),
            attempts=meeting.launch_attempts,
            error=error,
        )
    db.commit()


def run_forever() -> None:
    settings = get_settings()
    while True:
        db = SessionLocal()
        try:
            for meeting in due_meetings(db, settings.bot_launch_lead_seconds):
                if not claim_meeting(db, meeting.id):
                    continue
                # Each meeting's launch is isolated so one failure can't
                # also skip _handle_launch_failure for itself (the old bug)
                # or abort processing of the other due meetings this tick.
                try:
                    launch_container(settings, meeting.id)
                except Exception as exc:
                    logger.error("bot_container_launch_failed", meeting_id=str(meeting.id), exc_info=True)
                    _handle_launch_failure(db, meeting, str(exc))
        except Exception:
            logger.error("bot_launcher_tick_failed", exc_info=True)
        finally:
            db.close()
        time.sleep(15)
