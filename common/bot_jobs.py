"""Hook points into the bot launcher.

Calendar sync calls these when a meeting is created, rescheduled, or cancelled.
Phase 2 (the meeting bot + its scheduler) will replace these bodies with real
container scheduling; call sites elsewhere never need to change.
"""
from common.logging import get_logger
from common.models import Meeting

logger = get_logger(__name__)


def schedule_job(meeting: Meeting) -> None:
    logger.info(
        "bot_job_schedule_stub", meeting_id=str(meeting.id), scheduled_at=str(meeting.scheduled_at)
    )


def reschedule_job(meeting: Meeting) -> None:
    logger.info(
        "bot_job_reschedule_stub", meeting_id=str(meeting.id), scheduled_at=str(meeting.scheduled_at)
    )


def cancel_job(meeting: Meeting) -> None:
    logger.info("bot_job_cancel_stub", meeting_id=str(meeting.id))
