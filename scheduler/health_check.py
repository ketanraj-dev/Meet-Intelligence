"""Weekly health check for the bot account pool.

Rotation (bot_pool/service.py's last_used_at ordering) keeps every
account's Google session exercised by real meetings, but an account can sit
idle for a while if there just aren't enough meetings to go around, and a
saved session can expire silently (password change, a Google security
checkpoint, long inactivity) with no error until a real meeting tries to
use it and fails to join. This periodically launches a one-shot bot
container per currently-idle account to check.
"""
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.config import Settings, get_settings
from common.db import SessionLocal
from common.logging import get_logger
from common.models import BotAccount, BotAccountStatus
from scheduler.bot_launcher import launch_health_check_container

logger = get_logger(__name__)


def accounts_due_for_health_check(db: Session, interval_seconds: int) -> list[BotAccount]:
    """Only ever FREE accounts — see launch_health_check_container's
    docstring for why an IN_USE account can't be checked concurrently.
    DISABLED accounts are skipped too: there's nothing to alert on for an
    account nobody intends to use. A never-checked account (NULL) is
    always due."""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=interval_seconds)
    stmt = select(BotAccount).where(
        BotAccount.status == BotAccountStatus.FREE,
        (BotAccount.last_health_check_at.is_(None)) | (BotAccount.last_health_check_at < cutoff),
    )
    return list(db.scalars(stmt))


def run_forever() -> None:
    settings: Settings = get_settings()
    while True:
        db = SessionLocal()
        try:
            for account in accounts_due_for_health_check(db, settings.bot_health_check_interval_seconds):
                try:
                    launch_health_check_container(settings, account.id)
                except Exception:
                    logger.error(
                        "bot_account_health_check_launch_failed", account_id=str(account.id), exc_info=True
                    )
        except Exception:
            logger.error("health_check_tick_failed", exc_info=True)
        finally:
            db.close()
        time.sleep(settings.bot_health_check_poll_seconds)
