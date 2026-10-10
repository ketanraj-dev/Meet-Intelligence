"""Entry point for the `scheduler` container.

Runs the calendar poller and the bot-account reaper as independent background
loops. Phase 2 adds a third loop here that launches bot containers at
scheduled_at - 60s (and the launcher will call bot_pool.service.checkout_or_mark_unavailable).
"""
import threading
import time

from bot_pool.service import reap_stale
from calendar_sync.poller import run_forever as run_calendar_poller
from common.config import get_settings
from common.db import SessionLocal
from common.logging import configure_logging, get_logger

logger = get_logger(__name__)


def _run_reaper_forever() -> None:
    settings = get_settings()
    while True:
        db = SessionLocal()
        try:
            released = reap_stale(db, settings.bot_heartbeat_timeout_seconds)
            if released:
                logger.info("bot_pool_reaped", count=len(released))
        except Exception:
            logger.error("bot_pool_reap_failed", exc_info=True)
            db.rollback()
        finally:
            db.close()
        time.sleep(30)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    threading.Thread(target=run_calendar_poller, daemon=True, name="calendar-poller").start()
    threading.Thread(target=_run_reaper_forever, daemon=True, name="bot-pool-reaper").start()

    logger.info("scheduler_started")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
