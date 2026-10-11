"""Minimal Redis queue producer.

Phase 3 adds the real consumer (Celery or RQ, TBD — see the GPU-queue
convention noted in the README). For now this just accumulates meeting_ids
so nothing's lost once a worker exists to drain it.
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
