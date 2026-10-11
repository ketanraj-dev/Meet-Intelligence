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
    if not settings.host_project_dir:
        logger.error("bot_launch_skipped_no_host_project_dir", meeting_id=str(meeting_id))
        return

    client = docker.from_env()
    container_name = f"meet-bot-{meeting_id}"
    try:
        client.containers.run(
            settings.bot_image,
            name=container_name,
            detach=True,
            remove=True,
            network=settings.docker_network,
            # Docker's default /dev/shm (64MB) is too small for Chromium and
            # causes "Aw, Snap!" tab crashes — mirrors the `bot` service's
            # shm_size in docker-compose.yml for manually-run bots.
            shm_size="2g",
            # Grace period before SIGKILL — must outlast the SIGTERM handler
            # leaving the call, stopping ffmpeg, and uploading the recording.
            # Mirrors stop_grace_period on the `bot` service in docker-compose.yml.
            stop_timeout=settings.bot_stop_timeout_seconds,
            environment=_bot_environment(settings, meeting_id),
            volumes={
                f"{settings.host_project_dir}/bot-profiles": {"bind": "/bot-profiles", "mode": "rw"},
                f"{settings.host_project_dir}/recordings": {"bind": "/recordings", "mode": "rw"},
            },
        )
        logger.info("bot_container_launched", meeting_id=str(meeting_id), container=container_name)
    except docker.errors.APIError:
        logger.error("bot_container_launch_failed", meeting_id=str(meeting_id), exc_info=True)


def run_forever() -> None:
    settings = get_settings()
    while True:
        db = SessionLocal()
        try:
            for meeting in due_meetings(db, settings.bot_launch_lead_seconds):
                if claim_meeting(db, meeting.id):
                    launch_container(settings, meeting.id)
        except Exception:
            logger.error("bot_launcher_tick_failed", exc_info=True)
        finally:
            db.close()
        time.sleep(15)
