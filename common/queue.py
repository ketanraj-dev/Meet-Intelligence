"""Minimal Redis queue producer.

processing_queue_name (Phase 3's worker consumes it): pushed by bot.py
after a meeting finishes recording. transcription_queue_name (Phase 4's
asr worker consumes it): pushed by worker/main.py after diarization
succeeds. Two separate queues/consumers on purpose — see
asr/transcription.py's module docstring (pyannote and Indic-Transcribe/
Whisper must not both be loaded in one container at peak).
"""
import json
import uuid

import redis

from common.config import get_settings

_client: redis.Redis | None = None


def _get_client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(get_settings().redis_url)
    return _client


def enqueue_processing(meeting_id: uuid.UUID) -> None:
    _get_client().lpush(
        get_settings().processing_queue_name, json.dumps({"meeting_id": str(meeting_id)})
    )


def enqueue_transcription(meeting_id: uuid.UUID) -> None:
    _get_client().lpush(
        get_settings().transcription_queue_name, json.dumps({"meeting_id": str(meeting_id)})
    )
