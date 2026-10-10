import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import IntegrityError

from bot_pool.service import (
    cancel_reservation,
    checkout_account,
    checkout_or_mark_unavailable,
    get_reservation,
    reap_stale,
    release_account,
    reschedule_reservation,
    reserve_account,
)
from common.models import BotAccount, BotAccountStatus, BotReservation, Meeting, MeetingStatus
from tests.conftest import TestingSessionLocal


def _make_meeting(db, **kwargs):
    meeting = Meeting(
        meet_url=kwargs.pop("meet_url", "https://meet.google.com/abc-defg-hij"),
        scheduled_at=kwargs.pop("scheduled_at", datetime.now(timezone.utc).replace(tzinfo=None)),
        status=kwargs.pop("status", MeetingStatus.SCHEDULED),
        **kwargs,
    )
    db.add(meeting)
    db.flush()
    return meeting


def test_checkout_prefers_already_reserved_account(db_session):
    account_a = BotAccount(email="a@example.com", profile_path="/p/a")
    account_b = BotAccount(email="b@example.com", profile_path="/p/b")
    db_session.add_all([account_a, account_b])
    meeting = _make_meeting(db_session)
    db_session.commit()

    reserved = reserve_account(db_session, meeting)
    db_session.commit()
    assert reserved is not None

    checked_out = checkout_account(db_session, meeting)
    db_session.commit()

    assert checked_out.id == reserved.id
    assert checked_out.status == BotAccountStatus.IN_USE


def test_checkout_falls_back_to_any_free_account_when_none_reserved(db_session):
    account = BotAccount(email="a@example.com", profile_path="/p/a")
    db_session.add(account)
    meeting = _make_meeting(db_session)
    db_session.commit()

    checked_out = checkout_account(db_session, meeting)
    db_session.commit()

    assert checked_out is not None
    assert checked_out.email == "a@example.com"
    assert checked_out.status == BotAccountStatus.IN_USE
    assert checked_out.current_meeting_id == meeting.id


def test_checkout_fallback_skips_account_with_overlapping_reservation(db_session):
    account1 = BotAccount(email="a@example.com", profile_path="/p/a")
    account2 = BotAccount(email="b@example.com", profile_path="/p/b")
    db_session.add_all([account1, account2])
    meeting_other = _make_meeting(
        db_session, meet_url="https://meet.google.com/aaa-0007-aaa", scheduled_at=datetime(2026, 11, 5, 10, 0, 0)
    )
    meeting_now = _make_meeting(
        db_session, meet_url="https://meet.google.com/aaa-0008-aaa", scheduled_at=datetime(2026, 11, 5, 10, 5, 0)
    )
    db_session.commit()

    reserved = reserve_account(db_session, meeting_other)
    db_session.commit()
    blocked_account_id = reserved.id

    # meeting_now has no reservation of its own; its window overlaps meeting_other's,
    # so checkout must skip the account reserved for meeting_other.
    checked_out = checkout_account(db_session, meeting_now)
    db_session.commit()

    assert checked_out is not None
    assert checked_out.id != blocked_account_id


def test_release_allows_account_reuse(db_session):
    account = BotAccount(email="a@example.com", profile_path="/p/a")
    db_session.add(account)
    meeting = _make_meeting(db_session)
    db_session.commit()

    checked_out = checkout_account(db_session, meeting)
    db_session.commit()
    assert checked_out is not None

    release_account(db_session, checked_out.id)
    db_session.commit()

    meeting2 = _make_meeting(db_session)
    db_session.commit()
    checked_out_again = checkout_account(db_session, meeting2)
    db_session.commit()

    assert checked_out_again is not None
    assert checked_out_again.id == checked_out.id


def test_exhausted_pool_marks_meeting_no_bot_available(db_session):
    meeting = _make_meeting(db_session)
    db_session.commit()

    account = checkout_or_mark_unavailable(db_session, meeting)
    db_session.commit()

    assert account is None
    assert meeting.status == MeetingStatus.NO_BOT_AVAILABLE


def test_reserve_returns_none_when_pool_exhausted(db_session):
    account = BotAccount(email="a@example.com", profile_path="/p/a")
    db_session.add(account)
    meeting_a = _make_meeting(db_session, meet_url="https://meet.google.com/aaa-aaaa-aaa")
    meeting_b = _make_meeting(db_session, meet_url="https://meet.google.com/bbb-bbbb-bbb")
    db_session.commit()

    first = reserve_account(db_session, meeting_a)
    db_session.commit()
    assert first is not None

    second = reserve_account(db_session, meeting_b)
    db_session.commit()
    assert second is None


def test_reserve_two_non_overlapping_meetings_on_one_account(db_session):
    account = BotAccount(email="a@example.com", profile_path="/p/a")
    db_session.add(account)
    meeting_morning = _make_meeting(
        db_session, meet_url="https://meet.google.com/aaa-0001-aaa", scheduled_at=datetime(2026, 11, 1, 9, 0, 0)
    )
    meeting_afternoon = _make_meeting(
        db_session, meet_url="https://meet.google.com/aaa-0002-aaa", scheduled_at=datetime(2026, 11, 1, 14, 0, 0)
    )
    db_session.commit()

    first = reserve_account(db_session, meeting_morning)
    db_session.commit()
    second = reserve_account(db_session, meeting_afternoon)
    db_session.commit()

    assert first is not None and second is not None
    assert first.id == second.id == account.id

    reservations = (
        db_session.query(BotReservation).filter(BotReservation.bot_account_id == account.id).all()
    )
    assert len(reservations) == 2


def test_overlap_rejected_at_db_level(db_session):
    """Proves the EXCLUDE constraint itself rejects overlapping windows on
    the same account, independent of any application-level logic."""
    account = BotAccount(email="a@example.com", profile_path="/p/a")
    db_session.add(account)
    meeting1 = _make_meeting(db_session, meet_url="https://meet.google.com/aaa-0003-aaa")
    meeting2 = _make_meeting(db_session, meet_url="https://meet.google.com/aaa-0004-aaa")
    db_session.commit()

    window1 = Range(
        lower=datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc),
        upper=datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc),
        bounds="[)",
    )
    window2 = Range(
        lower=datetime(2026, 11, 1, 9, 30, tzinfo=timezone.utc),
        upper=datetime(2026, 11, 1, 10, 30, tzinfo=timezone.utc),
        bounds="[)",
    )

    db_session.add(BotReservation(bot_account_id=account.id, meeting_id=meeting1.id, window=window1))
    db_session.commit()

    db_session.add(BotReservation(bot_account_id=account.id, meeting_id=meeting2.id, window=window2))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_reschedule_moves_reservation_in_place_same_account(db_session):
    account = BotAccount(email="a@example.com", profile_path="/p/a")
    db_session.add(account)
    meeting = _make_meeting(db_session, scheduled_at=datetime(2026, 11, 2, 9, 0, 0))
    db_session.commit()

    reserve_account(db_session, meeting)
    db_session.commit()
    reservation_id = get_reservation(db_session, meeting.id).id

    meeting.scheduled_at = datetime(2026, 11, 2, 11, 0, 0)
    db_session.commit()

    result_account = reschedule_reservation(db_session, meeting)
    db_session.commit()

    assert result_account.id == account.id
    rows = db_session.query(BotReservation).filter(BotReservation.meeting_id == meeting.id).all()
    assert len(rows) == 1
    assert rows[0].id == reservation_id  # same row, moved in place


def test_reschedule_is_idempotent_no_duplicate_reservation(db_session):
    account = BotAccount(email="a@example.com", profile_path="/p/a")
    db_session.add(account)
    meeting = _make_meeting(db_session, scheduled_at=datetime(2026, 11, 3, 9, 0, 0))
    db_session.commit()

    reserve_account(db_session, meeting)
    db_session.commit()

    reschedule_reservation(db_session, meeting)
    db_session.commit()
    reschedule_reservation(db_session, meeting)
    db_session.commit()

    rows = db_session.query(BotReservation).filter(BotReservation.meeting_id == meeting.id).all()
    assert len(rows) == 1


def test_reschedule_into_conflict_swaps_account(db_session):
    account1 = BotAccount(email="a@example.com", profile_path="/p/a")
    account2 = BotAccount(email="b@example.com", profile_path="/p/b")
    db_session.add_all([account1, account2])
    meeting_blocking = _make_meeting(
        db_session, meet_url="https://meet.google.com/aaa-0005-aaa", scheduled_at=datetime(2026, 11, 4, 12, 0, 0)
    )
    meeting_a = _make_meeting(
        db_session, meet_url="https://meet.google.com/aaa-0006-aaa", scheduled_at=datetime(2026, 11, 4, 9, 0, 0)
    )
    db_session.commit()

    reserve_account(db_session, meeting_blocking)
    db_session.commit()
    blocking_account_id = get_reservation(db_session, meeting_blocking.id).bot_account_id

    reserve_account(db_session, meeting_a)
    db_session.commit()
    # 9am doesn't overlap noon, so meeting_a lands on the same (first-candidate) account
    assert get_reservation(db_session, meeting_a.id).bot_account_id == blocking_account_id

    # Move meeting_a to overlap meeting_blocking's window -> same account conflicts
    meeting_a.scheduled_at = datetime(2026, 11, 4, 12, 5, 0)
    db_session.commit()

    new_account = reschedule_reservation(db_session, meeting_a)
    db_session.commit()

    assert new_account is not None
    assert new_account.id != blocking_account_id
    assert get_reservation(db_session, meeting_a.id).bot_account_id == new_account.id
    # meeting_blocking's reservation is untouched
    assert get_reservation(db_session, meeting_blocking.id).bot_account_id == blocking_account_id


def test_cancel_reservation_deletes_row_and_frees_live_state(db_session):
    account = BotAccount(email="a@example.com", profile_path="/p/a")
    db_session.add(account)
    meeting = _make_meeting(db_session)
    db_session.commit()

    checkout_account(db_session, meeting)
    db_session.commit()

    released = cancel_reservation(db_session, meeting.id)
    db_session.commit()

    assert released is not None
    assert get_reservation(db_session, meeting.id) is None
    db_session.refresh(account)
    assert account.status == BotAccountStatus.FREE
    assert account.current_meeting_id is None


def test_reaper_releases_stale_heartbeat_and_fails_meeting(db_session):
    account = BotAccount(email="a@example.com", profile_path="/p/a")
    db_session.add(account)
    meeting = _make_meeting(db_session, status=MeetingStatus.IN_CALL)
    db_session.commit()

    checked_out = checkout_account(db_session, meeting)
    checked_out.last_heartbeat_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=600)
    db_session.commit()

    reaped = reap_stale(db_session, timeout_seconds=180)
    db_session.commit()

    assert len(reaped) == 1
    db_session.refresh(checked_out)
    db_session.refresh(meeting)
    assert checked_out.status == BotAccountStatus.FREE
    assert checked_out.current_meeting_id is None
    assert meeting.status == MeetingStatus.FAILED
    assert meeting.error == "bot heartbeat timed out"


def test_reaper_ignores_fresh_heartbeat(db_session):
    account = BotAccount(email="a@example.com", profile_path="/p/a")
    db_session.add(account)
    meeting = _make_meeting(db_session, status=MeetingStatus.IN_CALL)
    db_session.commit()

    checkout_account(db_session, meeting)
    db_session.commit()

    reaped = reap_stale(db_session, timeout_seconds=180)
    db_session.commit()

    assert reaped == []


def test_concurrent_checkout_never_double_assigns_an_account(db_session):
    num_accounts = 3
    num_meetings = 10

    for i in range(num_accounts):
        db_session.add(BotAccount(email=f"bot{i}@example.com", profile_path=f"/p/{i}"))
    meeting_ids = []
    for i in range(num_meetings):
        meeting = _make_meeting(db_session, meet_url=f"https://meet.google.com/aaa-{i:04d}-aaa")
        meeting_ids.append(meeting.id)
    db_session.commit()

    results = [None] * num_meetings
    barrier = threading.Barrier(num_meetings)

    def worker(index, meeting_id):
        session = TestingSessionLocal()
        try:
            barrier.wait()
            meeting = session.get(Meeting, meeting_id)
            account = checkout_account(session, meeting)
            session.commit()
            results[index] = account.id if account is not None else None
        finally:
            session.close()

    threads = [
        threading.Thread(target=worker, args=(i, meeting_ids[i])) for i in range(num_meetings)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assigned = [r for r in results if r is not None]
    assert len(assigned) == num_accounts
    assert len(set(assigned)) == num_accounts  # no account assigned twice
    assert results.count(None) == num_meetings - num_accounts
