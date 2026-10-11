from datetime import datetime, timedelta, timezone

import pytest

from common.config import get_settings
from common.models import Meeting, MeetingStatus
from scheduler.bot_launcher import claim_meeting, due_meetings, launch_container


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _make_meeting(db, **kwargs) -> Meeting:
    meeting = Meeting(
        meet_url=kwargs.pop("meet_url", "https://meet.google.com/abc-defg-hij"),
        scheduled_at=kwargs.pop("scheduled_at", _now()),
        status=kwargs.pop("status", MeetingStatus.SCHEDULED),
        **kwargs,
    )
    db.add(meeting)
    db.commit()
    return meeting


def test_claim_meeting_flips_scheduled_to_joining_exactly_once(db_session):
    meeting = _make_meeting(db_session)

    assert claim_meeting(db_session, meeting.id) is True
    db_session.refresh(meeting)
    assert meeting.status == MeetingStatus.JOINING

    # a second claim attempt (e.g. a concurrent/overlapping launcher tick) is a no-op
    assert claim_meeting(db_session, meeting.id) is False


def test_due_meetings_only_returns_scheduled_within_lead_window(db_session):
    due_soon = _make_meeting(
        db_session, meet_url="https://meet.google.com/aaa-0001-aaa", scheduled_at=_now() + timedelta(seconds=30)
    )
    _make_meeting(  # too far in the future
        db_session, meet_url="https://meet.google.com/aaa-0002-aaa", scheduled_at=_now() + timedelta(hours=1)
    )
    _make_meeting(  # already past the grace cutoff
        db_session, meet_url="https://meet.google.com/aaa-0003-aaa", scheduled_at=_now() - timedelta(minutes=30)
    )
    _make_meeting(  # right time window, but not scheduled anymore
        db_session,
        meet_url="https://meet.google.com/aaa-0004-aaa",
        scheduled_at=_now() + timedelta(seconds=10),
        status=MeetingStatus.JOINING,
    )

    due = due_meetings(db_session, lead_seconds=60)

    assert [m.id for m in due] == [due_soon.id]


def test_launch_container_skips_without_host_project_dir(db_session, monkeypatch, caplog):
    monkeypatch.setenv("HOST_PROJECT_DIR", "")
    get_settings.cache_clear()
    settings = get_settings()

    # Should return early (logging an error) without ever touching the
    # Docker SDK — if it tried, this would raise since there's no real
    # docker.sock available in the test environment's assumptions here.
    launch_container(settings, "00000000-0000-0000-0000-000000000000")
