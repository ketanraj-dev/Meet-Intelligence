from datetime import datetime

from bot_pool.service import get_reservation, reserve_account
from calendar_sync.guests import remove_bot_guest, sync_bot_guest
from common.models import BotAccount, Meeting, MeetingStatus
from tests.fakes import FakeCalendarService


def _make_meeting(db, **kwargs):
    meeting = Meeting(
        meet_url=kwargs.pop("meet_url", "https://meet.google.com/abc-defg-hij"),
        scheduled_at=kwargs.pop("scheduled_at", datetime(2026, 11, 10, 9, 0, 0)),
        status=kwargs.pop("status", MeetingStatus.SCHEDULED),
        google_event_id=kwargs.pop("google_event_id", "evt-1"),
        owner_email=kwargs.pop("owner_email", "it@fincelerate.com"),
        **kwargs,
    )
    db.add(meeting)
    db.flush()
    return meeting


def test_sync_bot_guest_adds_new_account_as_attendee(db_session):
    db_session.add(BotAccount(email="fincelerate.notetaker1@gmail.com", profile_path="/p/1"))
    meeting = _make_meeting(db_session)
    db_session.commit()

    service = FakeCalendarService(get_responses={"evt-1": {"attendees": [{"email": "it@fincelerate.com"}]}})
    sync_bot_guest(db_session, meeting, calendar_id="primary", service=service)
    db_session.commit()

    assert len(service.events().patch_calls) == 1
    attendees = service.events().patch_calls[0]["body"]["attendees"]
    assert {"email": "fincelerate.notetaker1@gmail.com"} in attendees


def test_sync_bot_guest_is_idempotent_no_duplicate_patch_needed(db_session):
    db_session.add(BotAccount(email="fincelerate.notetaker1@gmail.com", profile_path="/p/1"))
    meeting = _make_meeting(db_session)
    db_session.commit()

    service = FakeCalendarService(
        get_responses={
            "evt-1": {"attendees": [{"email": "it@fincelerate.com"}, {"email": "fincelerate.notetaker1@gmail.com"}]}
        }
    )
    sync_bot_guest(db_session, meeting, calendar_id="primary", service=service)
    db_session.commit()

    # already present and reservation unchanged -> no patch call needed
    assert service.events().patch_calls == []


def test_sync_bot_guest_swaps_attendee_when_reservation_conflicts(db_session):
    account1 = BotAccount(email="fincelerate.notetaker1@gmail.com", profile_path="/p/1")
    account2 = BotAccount(email="fincelerate.notetaker2@gmail.com", profile_path="/p/2")
    db_session.add_all([account1, account2])
    meeting_blocking = _make_meeting(
        db_session,
        meet_url="https://meet.google.com/aaa-0010-aaa",
        google_event_id="evt-blocking",
        scheduled_at=datetime(2026, 11, 11, 12, 0, 0),
    )
    meeting_a = _make_meeting(
        db_session,
        meet_url="https://meet.google.com/aaa-0011-aaa",
        google_event_id="evt-a",
        scheduled_at=datetime(2026, 11, 11, 9, 0, 0),
    )
    db_session.commit()

    reserve_account(db_session, meeting_blocking)
    db_session.commit()
    blocking_account_id = get_reservation(db_session, meeting_blocking.id).bot_account_id
    blocking_email = db_session.get(BotAccount, blocking_account_id).email

    reserve_account(db_session, meeting_a)
    db_session.commit()
    assert get_reservation(db_session, meeting_a.id).bot_account_id == blocking_account_id

    # Move meeting_a to overlap meeting_blocking's window -> forces a swap.
    meeting_a.scheduled_at = datetime(2026, 11, 11, 12, 5, 0)
    db_session.commit()

    service = FakeCalendarService(
        get_responses={"evt-a": {"attendees": [{"email": "it@fincelerate.com"}, {"email": blocking_email}]}}
    )
    sync_bot_guest(db_session, meeting_a, calendar_id="primary", service=service)
    db_session.commit()

    new_account_id = get_reservation(db_session, meeting_a.id).bot_account_id
    assert new_account_id != blocking_account_id
    new_email = db_session.get(BotAccount, new_account_id).email

    assert len(service.events().patch_calls) == 1
    attendees = service.events().patch_calls[0]["body"]["attendees"]
    emails = {a["email"] for a in attendees}
    assert blocking_email not in emails
    assert new_email in emails

    # meeting_blocking's own reservation/guest is untouched
    assert get_reservation(db_session, meeting_blocking.id).bot_account_id == blocking_account_id


def test_remove_bot_guest_removes_only_matching_email(db_session):
    meeting = _make_meeting(db_session)
    db_session.commit()

    service = FakeCalendarService(
        get_responses={
            "evt-1": {
                "attendees": [
                    {"email": "it@fincelerate.com"},
                    {"email": "fincelerate.notetaker1@gmail.com"},
                    {"email": "client@acme.com"},
                ]
            }
        }
    )
    remove_bot_guest(meeting, "fincelerate.notetaker1@gmail.com", calendar_id="primary", service=service)

    assert len(service.events().patch_calls) == 1
    attendees = service.events().patch_calls[0]["body"]["attendees"]
    emails = {a["email"] for a in attendees}
    assert emails == {"it@fincelerate.com", "client@acme.com"}


def test_remove_bot_guest_no_patch_when_not_present(db_session):
    meeting = _make_meeting(db_session)
    db_session.commit()

    service = FakeCalendarService(get_responses={"evt-1": {"attendees": [{"email": "it@fincelerate.com"}]}})
    remove_bot_guest(meeting, "fincelerate.notetaker1@gmail.com", calendar_id="primary", service=service)

    assert service.events().patch_calls == []
