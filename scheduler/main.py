"""Entry point for the `scheduler` container.

Runs four independent background loops: the calendar poller, the bot-pool
reaper, the bot-launcher (launches one bot container per meeting at
scheduled_at - BOT_LAUNCH_LEAD_SECONDS), and the account health checker
(launches a one-shot bot container per idle account roughly weekly to
verify its saved Google session is still signed in).
"""
import threading
import time

from bot_pool.service import reap_stale
from calendar_sync.poller import run_forever as run_calendar_poller
from common.config import get_settings
from common.db import SessionLocal
from common.logging import configure_logging, get_logger
from scheduler.bot_launcher import run_forever as run_bot_launcher
from scheduler.health_check import run_forever as run_health_checker

logger = get_logger(__name__)


def _reap_once(db, settings) -> list:
    """Separated from the loop so it's testable: a previous version of this
    loop called reap_stale() but never db.commit()'d it — reap_stale() only
    flush()es by (documented) design, so every "successful" reap for the
    whole lifetime of this bug was silently rolled back by db.close(), and
    stuck accounts never actually got freed. Confirmed live: an account sat
    `status=in_use` with a last_heartbeat_at hours stale despite the reaper
    logging a successful reap for it every 30s."""
    released = reap_stale(db, settings.bot_heartbeat_timeout_seconds)
    db.commit()
    return released


def _run_reaper_forever() -> None:
    settings = get_settings()
    while True:
        db = SessionLocal()
        try:
            released = _reap_once(db, settings)
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
    threading.Thread(target=run_bot_launcher, daemon=True, name="bot-launcher").start()
    threading.Thread(target=run_health_checker, daemon=True, name="bot-account-health-checker").start()

    logger.info("scheduler_started")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
