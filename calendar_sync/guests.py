"""Optional: keep the reserved bot account as a guest on the calendar event.

Gated by AUTO_ADD_BOT_GUEST=true, requires the calendar.events scope. Never
touches anything on the event besides the attendee list.
"""
from sqlalchemy.orm import Session

from bot_pool.service import get_reservation, reschedule_reservation
from calendar_sync.client import build_calendar_service
from common.logging import get_logger
from common.models import BotAccount, Meeting

logger = get_logger(__name__)


def sync_bot_guest(db: Session, meeting: Meeting, calendar_id: str = "primary", service=None) -> None:
    """Reserves/reschedules an account for meeting, then makes sure the event's
    attendee list reflects whichever account is now reserved — adding it if
    new, and removing a previously-reserved account if `reschedule_reservation`
    had to swap accounts because of a conflict."""
    existing_reservation = get_reservation(db, meeting.id)
    old_email = (
        _account_email(db, existing_reservation.bot_account_id) if existing_reservation else None
    )

    new_account = reschedule_reservation(db, meeting)
    if new_account is None:
        logger.error("auto_add_bot_guest_no_account_available", meeting_id=str(meeting.id))
        return

    service = service or build_calendar_service(meeting.owner_email)
    event = service.events().get(calendarId=calendar_id, eventId=meeting.google_event_id).execute()
    attendees = event.get("attendees", [])

    changed = False
    if old_email and old_email != new_account.email:
        before = len(attendees)
        attendees = [a for a in attendees if a.get("email") != old_email]
        changed = changed or len(attendees) != before

    if not any(a.get("email") == new_account.email for a in attendees):
        attendees.append({"email": new_account.email})
        changed = True

    if changed:
        service.events().patch(
            calendarId=calendar_id, eventId=meeting.google_event_id, body={"attendees": attendees}
        ).execute()
        logger.info("bot_guest_synced", meeting_id=str(meeting.id), account_email=new_account.email)


def _account_email(db: Session, account_id) -> str | None:
    account = db.get(BotAccount, account_id)
    return account.email if account is not None else None


def remove_bot_guest(meeting: Meeting, bot_email: str, calendar_id: str = "primary", service=None) -> None:
    """Called on cancellation: removes bot_email from the event's attendees,
    leaving everything else untouched."""
    service = service or build_calendar_service(meeting.owner_email)
    event = service.events().get(calendarId=calendar_id, eventId=meeting.google_event_id).execute()
    attendees = event.get("attendees", [])
    filtered = [a for a in attendees if a.get("email") != bot_email]
    if len(filtered) != len(attendees):
        service.events().patch(
            calendarId=calendar_id, eventId=meeting.google_event_id, body={"attendees": filtered}
        ).execute()
        logger.info("bot_guest_removed", meeting_id=str(meeting.id), account_email=bot_email)
