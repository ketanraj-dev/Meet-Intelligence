"""Calendar -> meetings sync engine.

Pure DB + an injected `service` (a Google Calendar API client, or a test
double with the same `.events().list(...).execute()` / `.get()` shape). No
polling loop lives here — see poller.py — so events.watch push notifications
can replace the loop later without touching this logic.
"""
from datetime import datetime, timezone

from googleapiclient.errors import HttpError
from sqlalchemy import select
from sqlalchemy.orm import Session

from calendar_sync.client import build_calendar_service
from calendar_sync.mapping import (
    extract_client,
    extract_meet_url,
    matches_title_prefix,
    parse_expected_languages,
)
from common import bot_jobs
from common.config import Settings, get_settings
from common.logging import get_logger
from common.models import BotAccount, CalendarSyncState, Meeting, MeetingStatus, Participant, ParticipantRole

logger = get_logger(__name__)


def _is_gone(exc: HttpError) -> bool:
    status = getattr(getattr(exc, "resp", None), "status", None)
    return status == 410


def _auto_add_bot_guest_enabled(settings: Settings) -> bool:
    """AUTO_ADD_BOT_GUEST only makes sense when the bot actually signs in as
    the pool account (BOT_JOIN_MODE=signed_in) — a guest-mode bot never uses
    that Gmail identity to join, so inviting it as a calendar guest would be
    misleading. Takes precedence over the raw env flag."""
    return settings.auto_add_bot_guest and settings.bot_join_mode == "signed_in"


def _get_or_create_sync_state(db: Session, employee_email: str, calendar_id: str) -> CalendarSyncState:
    stmt = select(CalendarSyncState).where(
        CalendarSyncState.calendar_owner_email == employee_email,
        CalendarSyncState.calendar_id == calendar_id,
    )
    state = db.scalars(stmt).first()
    if state is None:
        state = CalendarSyncState(calendar_owner_email=employee_email, calendar_id=calendar_id)
        db.add(state)
        db.flush()
    return state


def _bot_account_emails(db: Session) -> set[str]:
    return set(db.scalars(select(BotAccount.email)).all())


def sync_calendar(
    db: Session, employee_email: str, calendar_id: str | None = None, service=None
) -> dict:
    """Runs one incremental (or full, if no token yet / token expired) sync
    pass for one employee's calendar. Returns counts for logging/tests."""
    settings = get_settings()
    calendar_id = calendar_id or settings.calendar_id
    service = service or build_calendar_service(employee_email)
    state = _get_or_create_sync_state(db, employee_email, calendar_id)
    bot_emails = _bot_account_emails(db)

    stats = {"created": 0, "updated": 0, "cancelled": 0, "skipped": 0}

    try:
        _run_sync_pages(db, service, state, employee_email, settings, bot_emails, stats)
    except HttpError as exc:
        if not _is_gone(exc):
            raise
        logger.warning(
            "calendar_sync_token_gone_full_resync",
            employee_email=employee_email,
            calendar_id=calendar_id,
        )
        state.sync_token = None
        db.flush()
        _run_sync_pages(db, service, state, employee_email, settings, bot_emails, stats)

    state.last_synced_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    return stats


def _run_sync_pages(
    db: Session,
    service,
    state: CalendarSyncState,
    employee_email: str,
    settings: Settings,
    bot_emails: set[str],
    stats: dict,
) -> None:
    page_token = None
    next_sync_token = None

    while True:
        list_kwargs = {
            "calendarId": state.calendar_id,
            "singleEvents": True,
            "showDeleted": True,
        }
        if page_token:
            list_kwargs["pageToken"] = page_token
        if state.sync_token:
            list_kwargs["syncToken"] = state.sync_token
        else:
            list_kwargs["timeMin"] = datetime.now(timezone.utc).isoformat()

        response = service.events().list(**list_kwargs).execute()

        for event in response.get("items", []):
            _apply_event(db, event, employee_email, state.calendar_id, settings, bot_emails, stats)

        next_sync_token = response.get("nextSyncToken", next_sync_token)
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    if next_sync_token:
        state.sync_token = next_sync_token
    db.flush()


def _find_existing_meeting(db: Session, ical_uid: str | None, meet_url: str | None) -> Meeting | None:
    if ical_uid:
        existing = db.scalars(select(Meeting).where(Meeting.google_ical_uid == ical_uid)).first()
        if existing is not None:
            return existing
    if meet_url:
        return db.scalars(select(Meeting).where(Meeting.meet_url == meet_url)).first()
    return None


def _ensure_agent_participant(db: Session, meeting: Meeting, employee_email: str) -> None:
    already_present = any(p.display_name == employee_email for p in meeting.participants)
    if not already_present:
        db.add(
            Participant(meeting_id=meeting.id, display_name=employee_email, role=ParticipantRole.AGENT)
        )
        db.flush()


def _ensure_client_participant(
    db: Session, meeting: Meeting, event: dict, employee_email: str, bot_emails: set[str]
) -> None:
    client = extract_client(event, employee_email, bot_emails)
    if client is None:
        return
    already_present = any(
        p.role == ParticipantRole.CLIENT and p.email == client["email"] for p in meeting.participants
    )
    if already_present:
        return
    db.add(
        Participant(
            meeting_id=meeting.id,
            display_name=client["name"] or client["email"],
            email=client["email"],
            role=ParticipantRole.CLIENT,
        )
    )
    db.flush()


def _event_start(event: dict) -> datetime | None:
    start = (event.get("start") or {})
    value = start.get("dateTime") or start.get("date")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _apply_event(
    db: Session,
    event: dict,
    employee_email: str,
    calendar_id: str,
    settings: Settings,
    bot_emails: set[str],
    stats: dict,
) -> None:
    ical_uid = event.get("iCalUID")

    if event.get("status") == "cancelled":
        existing = _find_existing_meeting(db, ical_uid, None)
        if existing is not None and existing.status not in (MeetingStatus.DONE, MeetingStatus.CANCELLED):
            existing.status = MeetingStatus.CANCELLED
            bot_jobs.cancel_job(existing)

            from bot_pool.service import cancel_reservation

            released_account = cancel_reservation(db, existing.id)
            db.flush()

            if released_account is not None and _auto_add_bot_guest_enabled(settings) and existing.google_event_id:
                from calendar_sync.guests import remove_bot_guest

                try:
                    remove_bot_guest(existing, released_account.email, calendar_id=calendar_id)
                except Exception:
                    logger.warning(
                        "remove_bot_guest_failed", meeting_id=str(existing.id), exc_info=True
                    )

            stats["cancelled"] += 1
        else:
            stats["skipped"] += 1
        return

    if not matches_title_prefix(event, settings.event_title_prefix):
        stats["skipped"] += 1
        return

    meet_url = extract_meet_url(event)
    if not meet_url:
        stats["skipped"] += 1
        return

    existing = _find_existing_meeting(db, ical_uid, meet_url)

    if existing is not None:
        if existing.owner_email and existing.owner_email != employee_email:
            _ensure_agent_participant(db, existing, employee_email)
            stats["skipped"] += 1
            return

        etag = event.get("etag")
        if etag and etag == existing.google_etag:
            stats["skipped"] += 1
            return

        if existing.owner_email is None:
            existing.owner_email = employee_email
        if existing.google_ical_uid is None and ical_uid:
            existing.google_ical_uid = ical_uid
        if existing.google_event_id is None:
            existing.google_event_id = event.get("id")

        new_scheduled_at = _event_start(event)
        time_changed = new_scheduled_at is not None and new_scheduled_at != existing.scheduled_at

        existing.meet_url = meet_url
        existing.google_etag = etag
        languages = parse_expected_languages(event.get("description"))
        if languages:
            existing.expected_languages = languages
        if new_scheduled_at is not None:
            existing.scheduled_at = new_scheduled_at
        db.flush()

        _ensure_client_participant(db, existing, event, employee_email, bot_emails)

        if time_changed:
            bot_jobs.reschedule_job(existing)
            if _auto_add_bot_guest_enabled(settings):
                from calendar_sync.guests import sync_bot_guest

                sync_bot_guest(db, existing, calendar_id=calendar_id)

        stats["updated"] += 1
        return

    scheduled_at = _event_start(event)
    if scheduled_at is None:
        stats["skipped"] += 1
        return

    meeting = Meeting(
        meet_url=meet_url,
        scheduled_at=scheduled_at,
        status=MeetingStatus.SCHEDULED,
        expected_languages=parse_expected_languages(event.get("description")),
        google_event_id=event.get("id"),
        google_ical_uid=ical_uid,
        google_etag=event.get("etag"),
        owner_email=employee_email,
    )
    db.add(meeting)
    db.flush()
    db.add(Participant(meeting_id=meeting.id, display_name=employee_email, role=ParticipantRole.HOST))
    db.flush()

    _ensure_client_participant(db, meeting, event, employee_email, bot_emails)

    bot_jobs.schedule_job(meeting)
    if _auto_add_bot_guest_enabled(settings):
        from calendar_sync.guests import sync_bot_guest

        sync_bot_guest(db, meeting, calendar_id=calendar_id)

    stats["created"] += 1
