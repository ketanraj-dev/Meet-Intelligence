from datetime import datetime
from types import SimpleNamespace

import asr.cli as cli_module
from common.models import Meeting, MeetingStatus, Participant, ParticipantRole, SpeakerTurn, TranscriptSegment


def _make_meeting(db, **kwargs):
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


def test_transcribe_meeting_cmd_success(db_session, monkeypatch, capsys):
    meeting = _make_meeting(db_session)
    monkeypatch.setattr(cli_module, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(cli_module, "transcribe_meeting", lambda db, m, settings, limit=None: 6)

    cli_module.transcribe_meeting_cmd(meeting.id)

    db_session.refresh(meeting)
    assert meeting.status == MeetingStatus.TRANSCRIBED
    out = capsys.readouterr().out
    assert "transcribed" in out
    assert "6 segments" in out


def test_transcribe_meeting_cmd_sets_failed_status_and_reraises_on_error(db_session, monkeypatch, capsys):
    meeting = _make_meeting(db_session)

    def _raises(db, m, settings, limit=None):
        raise RuntimeError("no turns")

    monkeypatch.setattr(cli_module, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(cli_module, "transcribe_meeting", _raises)

    raised = False
    try:
        cli_module.transcribe_meeting_cmd(meeting.id)
    except RuntimeError:
        raised = True
    assert raised

    db_session.refresh(meeting)
    assert meeting.status == MeetingStatus.FAILED
    assert meeting.error == "no turns"
    assert "FAILED" in capsys.readouterr().out


def test_transcribe_meeting_cmd_unknown_meeting(db_session, monkeypatch, capsys):
    import uuid

    monkeypatch.setattr(cli_module, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)

    cli_module.transcribe_meeting_cmd(uuid.uuid4())

    assert "no such meeting" in capsys.readouterr().out


def _turn(meeting_id, start_ms, end_ms, name="Ketan Raj", overlap=False):
    return SpeakerTurn(
        meeting_id=meeting_id,
        cluster_label="SPEAKER_00",
        participant_id="p1",
        speaker_name=name,
        mapping_confidence=1.0,
        start_ms=start_ms,
        end_ms=end_ms,
        overlap=overlap,
    )


def test_show_transcript_cmd_prints_formatted_lines_with_role(db_session, monkeypatch, capsys):
    meeting = _make_meeting(db_session)
    db_session.add(Participant(meeting_id=meeting.id, display_name="Ketan Raj", role=ParticipantRole.CLIENT))
    db_session.commit()

    turn = _turn(meeting.id, 1500, 6000)
    db_session.add(turn)
    db_session.commit()
    db_session.add(
        TranscriptSegment(
            meeting_id=meeting.id,
            speaker_turn_id=turn.id,
            start_ms=1500,
            end_ms=6000,
            detected_language="hi",
            language="hi",
            language_source="language_id",
            text="Namaste, kaise hain aap?",
            asr_model="indic_transcribe:bodhan-ai/indic-transcribe-flex",
        )
    )
    db_session.commit()

    monkeypatch.setattr(cli_module, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)

    cli_module.show_transcript_cmd(meeting.id)

    out = capsys.readouterr().out
    assert "[00:00:01.500] Ketan Raj (client) {lang=hi src=language_id}: Namaste, kaise hain aap?" in out


def test_show_transcript_cmd_language_tag_includes_lid_topk_when_present(db_session, monkeypatch, capsys):
    meeting = _make_meeting(db_session)
    turn = _turn(meeting.id, 0, 5000)
    db_session.add(turn)
    db_session.commit()
    db_session.add(
        TranscriptSegment(
            meeting_id=meeting.id,
            speaker_turn_id=turn.id,
            start_ms=0,
            end_ms=5000,
            detected_language="en",
            lid_topk=[["en", 0.91], ["hi", 0.05]],
            language="en",
            language_source="lid_en_high_confidence",
            text="hello there",
            asr_model="indic_transcribe:bodhan-ai/indic-transcribe-flex",
        )
    )
    db_session.commit()

    monkeypatch.setattr(cli_module, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)

    cli_module.show_transcript_cmd(meeting.id)

    out = capsys.readouterr().out
    assert "{lang=en src=lid_en_high_confidence det=en:0.91}" in out


def test_show_transcript_cmd_marks_low_confidence(db_session, monkeypatch, capsys):
    meeting = _make_meeting(db_session)
    turn = _turn(meeting.id, 0, 5000)
    db_session.add(turn)
    db_session.commit()
    db_session.add(
        TranscriptSegment(
            meeting_id=meeting.id,
            speaker_turn_id=turn.id,
            start_ms=0,
            end_ms=5000,
            language="hi",
            language_source="default_indic_mixed_mode",
            text="Hello Hello Hello Hello",
            asr_model="indic_transcribe:bodhan-ai/indic-transcribe-flex",
            low_confidence=True,
            low_confidence_reason="repetition_detected",
        )
    )
    db_session.commit()

    monkeypatch.setattr(cli_module, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)

    cli_module.show_transcript_cmd(meeting.id)

    assert "[low_confidence: repetition_detected]" in capsys.readouterr().out


def test_show_transcript_cmd_unknown_role_when_no_participant_match(db_session, monkeypatch, capsys):
    meeting = _make_meeting(db_session)
    turn = _turn(meeting.id, 0, 5000, name="Unknown speaker 1")
    db_session.add(turn)
    db_session.commit()
    db_session.add(
        TranscriptSegment(
            meeting_id=meeting.id,
            speaker_turn_id=turn.id,
            start_ms=0,
            end_ms=5000,
            language="hi",
            language_source="language_id",
            text="hello",
            asr_model="whisper:small",
        )
    )
    db_session.commit()

    monkeypatch.setattr(cli_module, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)

    cli_module.show_transcript_cmd(meeting.id)

    assert "(unknown) {lang=hi src=language_id}: hello" in capsys.readouterr().out


def test_show_transcript_cmd_marks_overlap(db_session, monkeypatch, capsys):
    meeting = _make_meeting(db_session)
    turn = _turn(meeting.id, 0, 5000, overlap=True)
    db_session.add(turn)
    db_session.commit()
    db_session.add(
        TranscriptSegment(
            meeting_id=meeting.id,
            speaker_turn_id=turn.id,
            start_ms=0,
            end_ms=5000,
            language="hi",
            language_source="language_id",
            text="hello",
            asr_model="whisper:small",
        )
    )
    db_session.commit()

    monkeypatch.setattr(cli_module, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)

    cli_module.show_transcript_cmd(meeting.id)

    assert "[overlap]" in capsys.readouterr().out


def test_show_transcript_cmd_handles_no_transcript(db_session, monkeypatch, capsys):
    meeting = _make_meeting(db_session)
    monkeypatch.setattr(cli_module, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)

    cli_module.show_transcript_cmd(meeting.id)

    assert "no transcript" in capsys.readouterr().out
