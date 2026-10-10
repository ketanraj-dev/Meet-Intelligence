"""Background loop: sync every watched calendar every CALENDAR_POLL_SECONDS.

Deliberately dumb (sleep-loop polling) and separate from sync.py's actual sync
logic, so swapping this for Google Calendar push notifications (events.watch)
later only means replacing this file.
"""
import time

from calendar_sync.sync import sync_calendar
from common.config import get_settings
from common.db import SessionLocal
from common.logging import get_logger

logger = get_logger(__name__)


def run_forever() -> None:
    settings = get_settings()
    employees = settings.employee_email_list
    if not employees:
        logger.warning("calendar_poller_no_employees_configured")

    while True:
        for employee_email in employees:
            db = SessionLocal()
            try:
                stats = sync_calendar(db, employee_email, settings.calendar_id)
                logger.info("calendar_sync_complete", employee_email=employee_email, **stats)
            except Exception:
                logger.error("calendar_sync_failed", employee_email=employee_email, exc_info=True)
                db.rollback()
            finally:
                db.close()
        time.sleep(settings.calendar_poll_seconds)
