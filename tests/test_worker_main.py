from datetime import datetime
from types import SimpleNamespace

import pytest

import worker.main as worker_main
from common.models import Meeting, MeetingStatus
from worker.main import MAX_DIARIZATION_ATTEMPTS, _handle_meeting


def _settings_stub(**overrides):
    base = dict(redis_url="redis://localhost:6379/0", processing_queue_name="test:processing")
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_recorded_meeting(db, **kwargs):
    meeting = Meeting(
        meet_url="https://meet.google.com/abc-defg-hij",
        scheduled_at=datetime(2026, 1, 1),
        status=kwargs.pop("status", MeetingStatus.RECORDED),
        audio_key="meetings/x/audio/",
        **kwargs,
    )
    db.add(meeting)
    db.commit()
    return meeting


def test_handle_meeting_marks_diarized_on_success(db_session, monkeypatch):
    from tests.conftest import TestingSessionLocal, fresh_from_db

    meeting = _make_recorded_meeting(db_session)
    meeting_id = meeting.id

    enqueued = []
    monkeypatch.setattr(worker_main, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(worker_main, "process_meeting", lambda db, m, settings: 3)
    monkeypatch.setattr(worker_main, "enqueue_transcription", lambda mid: enqueued.append(mid))

    _handle_meeting(meeting_id, _settings_stub())

    assert enqueued == [meeting_id]  # Phase 4's asr worker picks this meeting up next

    refreshed = fresh_from_db(Meeting, meeting_id)
    assert refreshed.status == MeetingStatus.DIARIZED


def test_handle_meeting_skips_a_meeting_not_in_recorded_status(db_session, monkeypatch):
    from tests.conftest import TestingSessionLocal, fresh_from_db

    meeting = _make_recorded_meeting(db_session, status=MeetingStatus.DIARIZED)
    meeting_id = meeting.id

    monkeypatch.setattr(worker_main, "SessionLocal", TestingSessionLocal)
    calls = []
    monkeypatch.setattr(worker_main, "process_meeting", lambda db, m, settings: calls.append(1))

    _handle_meeting(meeting_id, _settings_stub())

    assert calls == []  # never touched — already past `recorded`


def test_handle_meeting_reverts_to_recorded_and_requeues_under_the_attempt_cap(db_session, monkeypatch):
    from tests.conftest import TestingSessionLocal, fresh_from_db

    meeting = _make_recorded_meeting(db_session)
    meeting_id = meeting.id

    def _raises(db, m, settings):
        raise RuntimeError("boom")

    requeued = []
    monkeypatch.setattr(worker_main, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(worker_main, "process_meeting", _raises)
    monkeypatch.setattr(worker_main, "enqueue_processing", lambda mid: requeued.append(mid))
    monkeypatch.setattr(worker_main.time, "sleep", lambda _s: None)  # don't actually wait in tests

    _handle_meeting(meeting_id, _settings_stub())

    refreshed = fresh_from_db(Meeting, meeting_id)
    assert refreshed.status == MeetingStatus.RECORDED
    assert refreshed.diarization_attempts == 1
    assert refreshed.error == "boom"
    assert requeued == [meeting_id]


def test_handle_meeting_gives_up_after_max_attempts(db_session, monkeypatch):
    from tests.conftest import TestingSessionLocal, fresh_from_db

    meeting = _make_recorded_meeting(db_session, diarization_attempts=MAX_DIARIZATION_ATTEMPTS - 1)
    meeting_id = meeting.id

    def _raises(db, m, settings):
        raise RuntimeError("still broken")

    requeued = []
    monkeypatch.setattr(worker_main, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(worker_main, "process_meeting", _raises)
    monkeypatch.setattr(worker_main, "enqueue_processing", lambda mid: requeued.append(mid))
    monkeypatch.setattr(worker_main.time, "sleep", lambda _s: None)

    _handle_meeting(meeting_id, _settings_stub())

    refreshed = fresh_from_db(Meeting, meeting_id)
    assert refreshed.status == MeetingStatus.FAILED
    assert refreshed.diarization_attempts == MAX_DIARIZATION_ATTEMPTS
    assert requeued == []  # no further retry once permanently failed


def test_handle_meeting_logs_and_returns_for_unknown_meeting(monkeypatch):
    from tests.conftest import TestingSessionLocal, fresh_from_db
    import uuid

    monkeypatch.setattr(worker_main, "SessionLocal", TestingSessionLocal)
    _handle_meeting(uuid.uuid4(), _settings_stub())  # must not raise
