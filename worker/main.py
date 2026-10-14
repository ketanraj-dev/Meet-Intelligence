"""Processing worker entry point.

Consumes common.queue's processing_queue_name (a Redis list, pushed via
enqueue_processing at the end of a successful bot run — see bot/bot.py's
_finalize_and_release) and runs Phase 3 diarization for each meeting.

Status flow: recorded -> diarizing -> diarized (or failed, with retries —
see _handle_meeting). The model is loaded once here before the loop starts
(not lazily on the first job) so a broken HF_TOKEN/model-access problem
fails fast and loudly at startup rather than surfacing as a mysterious
first-job failure, and so every job (including the first) pays the same
predictable latency.
"""
import json
import time
import uuid

import redis

from common.config import Settings, get_settings
from common.db import SessionLocal
from common.logging import configure_logging, get_logger
from common.models import Meeting, MeetingStatus
from common.queue import enqueue_processing, enqueue_transcription
from worker.diarization import _get_pipeline, process_meeting

logger = get_logger(__name__)

# Mirrors bot_launcher.py's MAX_LAUNCH_ATTEMPTS convention: a failed
# attempt reverts status to `recorded` so it gets picked up again, up to
# this many times, before giving up and marking `failed`.
MAX_DIARIZATION_ATTEMPTS = 3
# Short pause before requeueing a failed meeting — avoids a tight
# fail-instantly-retry-instantly loop burning CPU on a deterministically
# broken job while still recovering quickly from a real transient failure
# (a dropped connection, storage hiccup, etc).
RETRY_BACKOFF_SECONDS = 10


def _handle_meeting(meeting_id: uuid.UUID, settings: Settings) -> None:
    db = SessionLocal()
    try:
        meeting = db.get(Meeting, meeting_id)
        if meeting is None:
            logger.error("diarization_meeting_not_found", meeting_id=str(meeting_id))
            return
        if meeting.status != MeetingStatus.RECORDED:
            logger.warning(
                "diarization_skipped_unexpected_status",
                meeting_id=str(meeting_id),
                status=meeting.status,
            )
            return

        meeting.status = MeetingStatus.DIARIZING
        meeting.error = None
        db.commit()

        try:
            turn_count = process_meeting(db, meeting, settings)
            meeting.status = MeetingStatus.DIARIZED
            db.commit()
            logger.info("diarization_complete", meeting_id=str(meeting_id), turns=turn_count)
            enqueue_transcription(meeting_id)
        except Exception as exc:
            db.rollback()
            meeting = db.get(Meeting, meeting_id)
            meeting.diarization_attempts += 1
            meeting.error = str(exc)
            if meeting.diarization_attempts < MAX_DIARIZATION_ATTEMPTS:
                meeting.status = MeetingStatus.RECORDED
                db.commit()
                logger.warning(
                    "diarization_failed_will_retry",
                    meeting_id=str(meeting_id),
                    attempt=meeting.diarization_attempts,
                    max_attempts=MAX_DIARIZATION_ATTEMPTS,
                    exc_info=True,
                )
                time.sleep(RETRY_BACKOFF_SECONDS)
                enqueue_processing(meeting_id)
            else:
                meeting.status = MeetingStatus.FAILED
                db.commit()
                logger.error(
                    "diarization_failed_permanently",
                    meeting_id=str(meeting_id),
                    attempts=meeting.diarization_attempts,
                    exc_info=True,
                )
    finally:
        db.close()


def run_forever(settings: Settings) -> None:
    client = redis.Redis.from_url(settings.redis_url)
    logger.info("worker_started", queue=settings.processing_queue_name)
    while True:
        item = client.brpop(settings.processing_queue_name, timeout=5)
        if item is None:
            continue
        _, raw = item
        try:
            payload = json.loads(raw)
            _handle_meeting(uuid.UUID(payload["meeting_id"]), settings)
        except Exception:
            logger.error("worker_tick_failed", exc_info=True)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    # Fail fast and loudly here (not on the first real job) if the model
    # can't load — a bad HF_TOKEN or un-accepted gated-model terms would
    # otherwise only surface as a confusing first-job diarization_failed.
    _get_pipeline(settings)
    run_forever(settings)


if __name__ == "__main__":
    main()
