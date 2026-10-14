"""ASR (transcription) worker entry point.

Consumes common.queue's transcription_queue_name (a Redis list, pushed by
worker/main.py right after a meeting's diarization succeeds). Separate
process/container/queue from worker/ (Phase 3's diarization) on purpose —
see asr/transcription.py's module docstring for why pyannote and the ASR
backends must never be loaded in the same process at once.

Status flow: diarized -> transcribing -> transcribed (or failed, with
retries — see _handle_meeting). Both ASR backends are loaded once here
before the loop starts (not lazily on the first job), same reasoning as
worker/main.py: a broken HF_TOKEN/model-access problem fails fast and
loudly at startup instead of surfacing as a confusing first-job failure.
"""
import json
import time
import uuid

import redis

from common.config import Settings, get_settings
from common.db import SessionLocal
from common.logging import configure_logging, get_logger
from common.models import Meeting, MeetingStatus
from common.queue import enqueue_transcription
from asr.transcription import IndicTranscribeASR, WhisperASR, transcribe_meeting

logger = get_logger(__name__)

MAX_TRANSCRIPTION_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 10


def _handle_meeting(meeting_id: uuid.UUID, settings: Settings) -> None:
    db = SessionLocal()
    try:
        meeting = db.get(Meeting, meeting_id)
        if meeting is None:
            logger.error("transcription_meeting_not_found", meeting_id=str(meeting_id))
            return
        if meeting.status != MeetingStatus.DIARIZED:
            logger.warning(
                "transcription_skipped_unexpected_status",
                meeting_id=str(meeting_id),
                status=meeting.status,
            )
            return

        meeting.status = MeetingStatus.TRANSCRIBING
        meeting.error = None
        db.commit()

        try:
            segment_count = transcribe_meeting(db, meeting, settings)
            meeting.status = MeetingStatus.TRANSCRIBED
            db.commit()
            logger.info("transcription_job_complete", meeting_id=str(meeting_id), segments=segment_count)
        except Exception as exc:
            db.rollback()
            meeting = db.get(Meeting, meeting_id)
            meeting.transcription_attempts += 1
            meeting.error = str(exc)
            if meeting.transcription_attempts < MAX_TRANSCRIPTION_ATTEMPTS:
                meeting.status = MeetingStatus.DIARIZED
                db.commit()
                logger.warning(
                    "transcription_failed_will_retry",
                    meeting_id=str(meeting_id),
                    attempt=meeting.transcription_attempts,
                    max_attempts=MAX_TRANSCRIPTION_ATTEMPTS,
                    exc_info=True,
                )
                time.sleep(RETRY_BACKOFF_SECONDS)
                enqueue_transcription(meeting_id)
            else:
                meeting.status = MeetingStatus.FAILED
                db.commit()
                logger.error(
                    "transcription_failed_permanently",
                    meeting_id=str(meeting_id),
                    attempts=meeting.transcription_attempts,
                    exc_info=True,
                )
    finally:
        db.close()


def run_forever(settings: Settings) -> None:
    client = redis.Redis.from_url(settings.redis_url)
    logger.info("asr_worker_started", queue=settings.transcription_queue_name)
    while True:
        item = client.brpop(settings.transcription_queue_name, timeout=5)
        if item is None:
            continue
        _, raw = item
        try:
            payload = json.loads(raw)
            _handle_meeting(uuid.UUID(payload["meeting_id"]), settings)
        except Exception:
            logger.error("asr_worker_tick_failed", exc_info=True)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    # Fail fast and loudly here (not on the first real job) if either
    # model can't load.
    IndicTranscribeASR(
        settings.indic_transcribe_model_id,
        settings.transcription_device,
        settings.indic_transcribe_mode,
        settings.asr_num_threads,
    )._pipeline()
    WhisperASR(
        settings.whisper_model_size, settings.transcription_device, settings.whisper_compute_type, settings.asr_num_threads
    )._model()
    logger.info("asr_worker_thread_config", asr_num_threads=settings.asr_num_threads)
    run_forever(settings)


if __name__ == "__main__":
    main()
