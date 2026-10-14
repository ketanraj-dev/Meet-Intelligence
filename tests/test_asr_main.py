from datetime import datetime
from types import SimpleNamespace

import asr.main as asr_main
from common.models import Meeting, MeetingStatus
from asr.main import MAX_TRANSCRIPTION_ATTEMPTS, _handle_meeting


def _settings_stub(**overrides):
    base = dict(redis_url="redis://localhost:6379/0", transcription_queue_name="test:transcription")
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_diarized_meeting(db, **kwargs):
    meeting = Meeting(
        meet_url="https://meet.google.com/abc-defg-hij",
        scheduled_at=datetime(2026, 1, 1),
        status=kwargs.pop("status", MeetingStatus.DIARIZED),
        audio_key="meetings/x/audio/",
        **kwargs,
    )
    db.add(meeting)
    db.commit()
    return meeting


def test_handle_meeting_marks_transcribed_on_success(db_session, monkeypatch):
    from tests.conftest import TestingSessionLocal, fresh_from_db

    meeting = _make_diarized_meeting(db_session)
    meeting_id = meeting.id

    monkeypatch.setattr(asr_main, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(asr_main, "transcribe_meeting", lambda db, m, settings: 5)

    _handle_meeting(meeting_id, _settings_stub())

    refreshed = fresh_from_db(Meeting, meeting_id)
    assert refreshed.status == MeetingStatus.TRANSCRIBED


def test_handle_meeting_skips_a_meeting_not_in_diarized_status(db_session, monkeypatch):
    from tests.conftest import TestingSessionLocal

    meeting = _make_diarized_meeting(db_session, status=MeetingStatus.TRANSCRIBED)
    meeting_id = meeting.id

    monkeypatch.setattr(asr_main, "SessionLocal", TestingSessionLocal)
    calls = []
    monkeypatch.setattr(asr_main, "transcribe_meeting", lambda db, m, settings: calls.append(1))

    _handle_meeting(meeting_id, _settings_stub())

    assert calls == []


def test_handle_meeting_reverts_to_diarized_and_requeues_under_the_attempt_cap(db_session, monkeypatch):
    from tests.conftest import TestingSessionLocal, fresh_from_db

    meeting = _make_diarized_meeting(db_session)
    meeting_id = meeting.id

    def _raises(db, m, settings):
        raise RuntimeError("boom")

    requeued = []
    monkeypatch.setattr(asr_main, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(asr_main, "transcribe_meeting", _raises)
    monkeypatch.setattr(asr_main, "enqueue_transcription", lambda mid: requeued.append(mid))
    monkeypatch.setattr(asr_main.time, "sleep", lambda _s: None)

    _handle_meeting(meeting_id, _settings_stub())

    refreshed = fresh_from_db(Meeting, meeting_id)
    assert refreshed.status == MeetingStatus.DIARIZED
    assert refreshed.transcription_attempts == 1
    assert refreshed.error == "boom"
    assert requeued == [meeting_id]


def test_handle_meeting_gives_up_after_max_attempts(db_session, monkeypatch):
    from tests.conftest import TestingSessionLocal, fresh_from_db

    meeting = _make_diarized_meeting(db_session, transcription_attempts=MAX_TRANSCRIPTION_ATTEMPTS - 1)
    meeting_id = meeting.id

    def _raises(db, m, settings):
        raise RuntimeError("still broken")

    requeued = []
    monkeypatch.setattr(asr_main, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(asr_main, "transcribe_meeting", _raises)
    monkeypatch.setattr(asr_main, "enqueue_transcription", lambda mid: requeued.append(mid))
    monkeypatch.setattr(asr_main.time, "sleep", lambda _s: None)

    _handle_meeting(meeting_id, _settings_stub())

    refreshed = fresh_from_db(Meeting, meeting_id)
    assert refreshed.status == MeetingStatus.FAILED
    assert refreshed.transcription_attempts == MAX_TRANSCRIPTION_ATTEMPTS
    assert requeued == []


def test_handle_meeting_logs_and_returns_for_unknown_meeting(monkeypatch):
    from tests.conftest import TestingSessionLocal
    import uuid

    monkeypatch.setattr(asr_main, "SessionLocal", TestingSessionLocal)
    _handle_meeting(uuid.uuid4(), _settings_stub())  # must not raise
