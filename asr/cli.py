"""Admin CLI for the ASR (transcription) worker.

    python -m asr.cli transcribe-meeting <meeting-id>
    python -m asr.cli show-transcript <meeting-id>

transcribe-meeting runs (or re-runs) transcription for one meeting
directly, bypassing the Redis queue. A single attempt, no automatic retry
(a human re-running the command IS the retry) — transcription_attempts is
left untouched so the worker's own retry budget isn't consumed by manual
runs, same convention as worker/cli.py's process-meeting.
"""
import argparse
import uuid

from sqlalchemy import select

from common.config import get_settings
from common.db import SessionLocal
from common.models import Meeting, MeetingStatus, Participant, ParticipantRole, TranscriptSegment
from asr.transcription import transcribe_meeting


def _format_timestamp(ms: int) -> str:
    total_seconds = ms / 1000
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"


def _role_for_speaker(db, meeting_id: uuid.UUID, speaker_name: str) -> str:
    """Best-effort only — speaker_turns.participant_id is NOT a foreign
    key to `participants` (different identity systems; see SpeakerTurn's
    model docstring), so there's no hard link to a role. Falls back to
    "unknown" when no calendar-invite participant's display_name matches
    (case-insensitively) this turn's resolved speaker name."""
    participants = db.scalars(select(Participant).where(Participant.meeting_id == meeting_id)).all()
    for participant in participants:
        if participant.display_name.strip().lower() == speaker_name.strip().lower():
            role = participant.role
            # The column is a plain String (no native DB enum), so a
            # freshly re-fetched row (e.g. after a commit expires the
            # session's objects) gives back a raw str, not a
            # ParticipantRole instance — only call .value when it's
            # actually still the enum.
            return role.value if isinstance(role, ParticipantRole) else role
    return "unknown"


def transcribe_meeting_cmd(meeting_id: uuid.UUID, limit: int | None = None) -> None:
    db = SessionLocal()
    try:
        meeting = db.get(Meeting, meeting_id)
        if meeting is None:
            print(f"no such meeting: {meeting_id}")
            return

        settings = get_settings()
        meeting.status = MeetingStatus.TRANSCRIBING
        meeting.error = None
        db.commit()

        try:
            segment_count = transcribe_meeting(db, meeting, settings, limit=limit)
            meeting.status = MeetingStatus.TRANSCRIBED
            db.commit()
            print(f"transcribed {meeting_id}: {segment_count} segments")
        except Exception as exc:
            db.rollback()
            meeting = db.get(Meeting, meeting_id)
            meeting.status = MeetingStatus.FAILED
            meeting.error = str(exc)
            db.commit()
            print(f"FAILED: {exc}")
            raise
    finally:
        db.close()


def _language_tag(segment: TranscriptSegment) -> str:
    """{lang=<used> src=<reason> det=<top LID lang>:<prob>} — the per-TURN
    decision (asr/transcription.py's decide_turn_language), shown next to
    every line so a wrong decision is visible without a DB query."""
    if segment.language is None:
        return f"{{lang=- src={segment.language_source}}}"
    detected = f" det={segment.detected_language}:{segment.lid_topk[0][1]:.2f}" if segment.lid_topk else ""
    return f"{{lang={segment.language} src={segment.language_source}{detected}}}"


def show_transcript_cmd(meeting_id: uuid.UUID) -> None:
    db = SessionLocal()
    try:
        segments = (
            db.query(TranscriptSegment)
            .filter(TranscriptSegment.meeting_id == meeting_id)
            .order_by(TranscriptSegment.start_ms)
            .all()
        )
        if not segments:
            print(f"no transcript for {meeting_id} (not transcribed yet, or diarization found no turns)")
            return
        for segment in segments:
            speaker_name = segment.speaker_turn.speaker_name
            role = _role_for_speaker(db, meeting_id, speaker_name)
            overlap_mark = " [overlap]" if segment.speaker_turn.overlap else ""
            lang_tag = _language_tag(segment)
            low_conf_mark = f" [low_confidence: {segment.low_confidence_reason}]" if segment.low_confidence else ""
            print(
                f"[{_format_timestamp(segment.start_ms)}] {speaker_name} ({role}) {lang_tag}: "
                f"{segment.text}{overlap_mark}{low_conf_mark}"
            )
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="ASR (transcription) worker admin CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    transcribe_p = sub.add_parser("transcribe-meeting")
    transcribe_p.add_argument("meeting_id")
    transcribe_p.add_argument(
        "--limit", type=int, default=None, help="transcribe only the first N turns (quick check, not a full run)"
    )

    show_p = sub.add_parser("show-transcript")
    show_p.add_argument("meeting_id")

    args = parser.parse_args()
    meeting_id = uuid.UUID(args.meeting_id)

    if args.command == "transcribe-meeting":
        transcribe_meeting_cmd(meeting_id, limit=args.limit)
    elif args.command == "show-transcript":
        show_transcript_cmd(meeting_id)


if __name__ == "__main__":
    main()
