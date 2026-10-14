import uuid
from datetime import datetime

import worker.cli as cli_module
from common.models import Meeting, MeetingStatus, SpeakerTurn


def _make_meeting(db, **kwargs):
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


def test_process_meeting_cmd_success(db_session, monkeypatch, capsys):
    meeting = _make_meeting(db_session)
    meeting_id = meeting.id

    monkeypatch.setattr(cli_module, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(cli_module, "process_meeting", lambda db, m, settings: 4)

    cli_module.process_meeting_cmd(meeting_id)

    db_session.refresh(meeting)
    assert meeting.status == MeetingStatus.DIARIZED
    assert meeting.error is None
    assert "diarized" in capsys.readouterr().out


def test_process_meeting_cmd_prints_turn_count(db_session, monkeypatch, capsys):
    meeting = _make_meeting(db_session)
    monkeypatch.setattr(cli_module, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(cli_module, "process_meeting", lambda db, m, settings: 7)

    cli_module.process_meeting_cmd(meeting.id)

    out = capsys.readouterr().out
    assert "7 turns" in out


def test_process_meeting_cmd_flags_fallback_naming_in_output(db_session, monkeypatch, capsys):
    meeting = _make_meeting(db_session)

    def _fake_process(db, m, settings):
        m.diarization_name_mapping_suspect = True
        return 2

    monkeypatch.setattr(cli_module, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(cli_module, "process_meeting", _fake_process)

    cli_module.process_meeting_cmd(meeting.id)

    assert "fallback" in capsys.readouterr().out


def test_process_meeting_cmd_sets_failed_status_and_reraises_on_error(db_session, monkeypatch, capsys):
    meeting = _make_meeting(db_session)
    meeting_id = meeting.id

    def _raises(db, m, settings):
        raise RuntimeError("no audio")

    monkeypatch.setattr(cli_module, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(cli_module, "process_meeting", _raises)

    try:
        cli_module.process_meeting_cmd(meeting_id)
        raised = False
    except RuntimeError:
        raised = True
    assert raised

    db_session.refresh(meeting)
    assert meeting.status == MeetingStatus.FAILED
    assert meeting.error == "no audio"
    assert "FAILED" in capsys.readouterr().out


def test_process_meeting_cmd_unknown_meeting_prints_message(db_session, monkeypatch, capsys):
    monkeypatch.setattr(cli_module, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)

    cli_module.process_meeting_cmd(uuid.uuid4())

    assert "no such meeting" in capsys.readouterr().out


def test_show_turns_cmd_prints_formatted_turns(db_session, monkeypatch, capsys):
    meeting = _make_meeting(db_session)
    db_session.add(
        SpeakerTurn(
            meeting_id=meeting.id,
            cluster_label="SPEAKER_00",
            participant_id="p1",
            speaker_name="Ketan Raj",
            mapping_confidence=0.875,
            start_ms=1500,
            end_ms=65000,
            overlap=False,
        )
    )
    db_session.add(
        SpeakerTurn(
            meeting_id=meeting.id,
            cluster_label="SPEAKER_01",
            participant_id=None,
            speaker_name="Unknown speaker 1",
            mapping_confidence=0.0,
            start_ms=65000,
            end_ms=67000,
            overlap=True,
        )
    )
    db_session.commit()

    monkeypatch.setattr(cli_module, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)

    cli_module.show_turns_cmd(meeting.id)

    out = capsys.readouterr().out
    assert "Ketan Raj" in out
    assert "confidence=0.88" in out
    assert "00:00:01.500 - 00:01:05.000" in out
    assert "Unknown speaker 1" in out
    assert "[overlap]" in out


def test_show_turns_cmd_handles_no_turns(db_session, monkeypatch, capsys):
    meeting = _make_meeting(db_session)
    monkeypatch.setattr(cli_module, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)

    cli_module.show_turns_cmd(meeting.id)

    assert "no turns" in capsys.readouterr().out
