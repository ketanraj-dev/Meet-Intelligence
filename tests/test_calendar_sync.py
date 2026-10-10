import pytest
from sqlalchemy import select

from bot_pool.service import get_reservation, reserve_account
from calendar_sync.sync import sync_calendar
from common import bot_jobs
from common.config import get_settings
from common.models import (
    BotAccount,
    CalendarSyncState,
    Meeting,
    MeetingStatus,
    ParticipantRole,
)
from tests.fakes import FakeCalendarService, make_event


@pytest.fixture(autouse=True)
def _reset_settings_cache(monkeypatch):
    # Tests must not depend on the real .env's live config (EVENT_TITLE_PREFIX,
    # AUTO_ADD_BOT_GUEST, etc.) — isolate to safe defaults; tests that exercise
    # one of these explicitly override it themselves.
    monkeypatch.setenv("EVENT_TITLE_PREFIX", "")
    monkeypatch.setenv("AUTO_ADD_BOT_GUEST", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_full_sync_creates_meeting_and_stores_sync_token(db_session):
    service = FakeCalendarService(
        pages=[{"items": [make_event(description="Agenda\nLanguage: hi, en")], "nextSyncToken": "tok-1"}]
    )

    stats = sync_calendar(db_session, "alice@example.com", "primary", service=service)

    assert stats == {"created": 1, "updated": 0, "cancelled": 0, "skipped": 0}

    meeting = db_session.scalars(select(Meeting)).one()
    assert meeting.meet_url == "https://meet.google.com/abc-defg-hij"
    assert meeting.owner_email == "alice@example.com"
    assert meeting.google_ical_uid == "ical-1"
    assert meeting.expected_languages == ["hi", "en"]
    assert meeting.status == MeetingStatus.SCHEDULED

    state = db_session.scalars(select(CalendarSyncState)).one()
    assert state.sync_token == "tok-1"
    assert state.last_synced_at is not None

    # first call had no token yet -> full sync (timeMin), not incremental
    assert "timeMin" in service.events().list_calls[0]
    assert "syncToken" not in service.events().list_calls[0]


def test_incremental_sync_uses_stored_token(db_session):
    db_session.add(
        CalendarSyncState(calendar_owner_email="alice@example.com", calendar_id="primary", sync_token="old-tok")
    )
    db_session.commit()

    service = FakeCalendarService(pages=[{"items": [], "nextSyncToken": "new-tok"}])
    sync_calendar(db_session, "alice@example.com", "primary", service=service)

    assert service.events().list_calls[0]["syncToken"] == "old-tok"
    state = db_session.scalars(select(CalendarSyncState)).one()
    assert state.sync_token == "new-tok"


def test_410_gone_triggers_full_resync(db_session):
    db_session.add(
        CalendarSyncState(calendar_owner_email="alice@example.com", calendar_id="primary", sync_token="expired-tok")
    )
    db_session.commit()

    service = FakeCalendarService(
        pages=[{"items": [make_event()], "nextSyncToken": "fresh-tok"}],
        fail_with_410_on_sync_token=True,
    )

    stats = sync_calendar(db_session, "alice@example.com", "primary", service=service)

    assert stats["created"] == 1
    # first call used the (now expired) syncToken and raised; retry used timeMin
    assert service.events().list_calls[0].get("syncToken") == "expired-tok"
    assert "timeMin" in service.events().list_calls[1]

    state = db_session.scalars(select(CalendarSyncState)).one()
    assert state.sync_token == "fresh-tok"


def test_title_prefix_filters_out_non_matching_events(db_session, monkeypatch):
    monkeypatch.setenv("EVENT_TITLE_PREFIX", "Client Call:")
    get_settings.cache_clear()

    service = FakeCalendarService(
        pages=[
            {
                "items": [
                    make_event(event_id="skip", ical_uid="ical-skip", summary="Internal Sync"),
                    make_event(event_id="keep", ical_uid="ical-keep", summary="Client Call: Acme"),
                ]
            }
        ]
    )

    stats = sync_calendar(db_session, "alice@example.com", "primary", service=service)

    assert stats == {"created": 1, "updated": 0, "cancelled": 0, "skipped": 1}
    meeting = db_session.scalars(select(Meeting)).one()
    assert meeting.google_ical_uid == "ical-keep"


def test_event_without_meet_link_is_skipped(db_session):
    service = FakeCalendarService(pages=[{"items": [make_event(hangout_link=None)]}])
    stats = sync_calendar(db_session, "alice@example.com", "primary", service=service)
    assert stats == {"created": 0, "updated": 0, "cancelled": 0, "skipped": 1}
    assert db_session.scalars(select(Meeting)).first() is None


def test_same_ical_uid_on_two_calendars_adds_agent_not_new_meeting(db_session):
    event = make_event(event_id="evt-alice", ical_uid="shared-uid")
    sync_calendar(
        db_session, "alice@example.com", "primary", service=FakeCalendarService(pages=[{"items": [event]}])
    )

    event_from_bob = make_event(event_id="evt-bob", ical_uid="shared-uid")
    sync_calendar(
        db_session, "bob@example.com", "primary", service=FakeCalendarService(pages=[{"items": [event_from_bob]}])
    )

    meetings = db_session.scalars(select(Meeting)).all()
    assert len(meetings) == 1

    meeting = meetings[0]
    assert meeting.owner_email == "alice@example.com"
    roles = {p.display_name: p.role for p in meeting.participants}
    assert roles["alice@example.com"] == ParticipantRole.HOST
    assert roles["bob@example.com"] == ParticipantRole.AGENT


def test_event_update_changes_fields_and_reschedules_on_time_change(db_session, monkeypatch):
    reschedule_calls = []
    monkeypatch.setattr(bot_jobs, "reschedule_job", lambda m: reschedule_calls.append(m.id))

    original = make_event(start="2026-11-01T10:00:00+05:30", etag='"etag-1"')
    sync_calendar(db_session, "alice@example.com", "primary", service=FakeCalendarService(pages=[{"items": [original]}]))
    meeting = db_session.scalars(select(Meeting)).one()
    original_start = meeting.scheduled_at

    updated = make_event(start="2026-11-01T11:30:00+05:30", etag='"etag-2"')
    sync_calendar(db_session, "alice@example.com", "primary", service=FakeCalendarService(pages=[{"items": [updated]}]))

    db_session.refresh(meeting)
    assert meeting.scheduled_at != original_start
    assert meeting.google_etag == '"etag-2"'
    assert reschedule_calls == [meeting.id]


def test_unchanged_etag_is_skipped(db_session):
    event = make_event(etag='"same-etag"')
    sync_calendar(db_session, "alice@example.com", "primary", service=FakeCalendarService(pages=[{"items": [event]}]))

    stats = sync_calendar(
        db_session, "alice@example.com", "primary", service=FakeCalendarService(pages=[{"items": [event]}])
    )
    assert stats == {"created": 0, "updated": 0, "cancelled": 0, "skipped": 1}


def test_cancelled_event_cancels_meeting_and_releases_reservation(db_session, monkeypatch):
    cancel_calls = []
    monkeypatch.setattr(bot_jobs, "cancel_job", lambda m: cancel_calls.append(m.id))

    event = make_event()
    sync_calendar(db_session, "alice@example.com", "primary", service=FakeCalendarService(pages=[{"items": [event]}]))
    meeting = db_session.scalars(select(Meeting)).one()

    db_session.add(BotAccount(email="bot1@example.com", profile_path="/profiles/bot1"))
    db_session.commit()
    account = reserve_account(db_session, meeting)
    db_session.commit()
    assert account is not None
    assert get_reservation(db_session, meeting.id) is not None

    cancelled_event = make_event(status="cancelled")
    stats = sync_calendar(
        db_session, "alice@example.com", "primary", service=FakeCalendarService(pages=[{"items": [cancelled_event]}])
    )

    db_session.refresh(meeting)
    assert stats["cancelled"] == 1
    assert meeting.status == MeetingStatus.CANCELLED
    assert cancel_calls == [meeting.id]
    assert get_reservation(db_session, meeting.id) is None


def test_client_participant_extracted_on_create(db_session):
    event = make_event(
        summary="30 min with Information (Acme Corp)",
        attendees=[
            {"email": "it@fincelerate.com"},
            {"email": "jane@acme.com", "displayName": "Jane Doe"},
        ],
    )
    sync_calendar(
        db_session, "it@fincelerate.com", "primary", service=FakeCalendarService(pages=[{"items": [event]}])
    )

    meeting = db_session.scalars(select(Meeting)).one()
    roles = {p.email: p.role for p in meeting.participants if p.email}
    assert roles["jane@acme.com"] == ParticipantRole.CLIENT
    client = next(p for p in meeting.participants if p.email == "jane@acme.com")
    assert client.display_name == "Acme Corp"


def test_client_name_falls_back_to_attendee_display_name(db_session):
    event = make_event(
        summary="30 min with Information",  # no (Client Name) in title
        attendees=[
            {"email": "it@fincelerate.com"},
            {"email": "jane@acme.com", "displayName": "Jane Doe"},
        ],
    )
    sync_calendar(
        db_session, "it@fincelerate.com", "primary", service=FakeCalendarService(pages=[{"items": [event]}])
    )

    meeting = db_session.scalars(select(Meeting)).one()
    client = next(p for p in meeting.participants if p.email == "jane@acme.com")
    assert client.display_name == "Jane Doe"


def test_client_extraction_excludes_bot_account_attendees(db_session):
    db_session.add(BotAccount(email="fincelerate.notetaker1@gmail.com", profile_path="/p/1"))
    db_session.commit()

    event = make_event(
        summary="30 min with Information (Acme Corp)",
        attendees=[
            {"email": "it@fincelerate.com"},
            {"email": "fincelerate.notetaker1@gmail.com"},
            {"email": "jane@acme.com", "displayName": "Jane Doe"},
        ],
    )
    sync_calendar(
        db_session, "it@fincelerate.com", "primary", service=FakeCalendarService(pages=[{"items": [event]}])
    )

    meeting = db_session.scalars(select(Meeting)).one()
    client_emails = {p.email for p in meeting.participants if p.role == ParticipantRole.CLIENT}
    assert client_emails == {"jane@acme.com"}
