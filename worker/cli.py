"""Admin CLI for the diarization worker.

    python -m worker.cli process-meeting <meeting-id>
    python -m worker.cli show-turns <meeting-id>

process-meeting runs (or re-runs) diarization for one meeting directly,
bypassing the Redis queue — useful for a first manual test or reprocessing
after a code/config change. A single attempt, no automatic retry (a human
re-running the command IS the retry); diarization_attempts is left
untouched so the worker's own retry budget isn't consumed by manual runs.
"""
import argparse
import uuid

from common.config import get_settings
from common.db import SessionLocal
from common.models import Meeting, MeetingStatus, SpeakerTurn
from worker.diarization import process_meeting


def _format_timestamp(ms: int) -> str:
    total_seconds = ms / 1000
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"


def process_meeting_cmd(meeting_id: uuid.UUID) -> None:
    db = SessionLocal()
    try:
        meeting = db.get(Meeting, meeting_id)
        if meeting is None:
            print(f"no such meeting: {meeting_id}")
            return

        settings = get_settings()
        meeting.status = MeetingStatus.DIARIZING
        meeting.error = None
        db.commit()

        try:
            turn_count = process_meeting(db, meeting, settings)
            meeting.status = MeetingStatus.DIARIZED
            db.commit()
            suspect = " (fallback Speaker A/B/... naming — speakers.jsonl had no speaking events)" if meeting.diarization_name_mapping_suspect else ""
            print(f"diarized {meeting_id}: {turn_count} turns{suspect}")
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


def show_turns_cmd(meeting_id: uuid.UUID) -> None:
    db = SessionLocal()
    try:
        turns = (
            db.query(SpeakerTurn)
            .filter(SpeakerTurn.meeting_id == meeting_id)
            .order_by(SpeakerTurn.start_ms)
            .all()
        )
        if not turns:
            print(f"no turns for {meeting_id} (not diarized yet, or diarization found nothing)")
            return
        for turn in turns:
            confidence = f" (confidence={turn.mapping_confidence:.2f})" if turn.mapping_confidence is not None else ""
            overlap_mark = " [overlap]" if turn.overlap else ""
            print(
                f"[{_format_timestamp(turn.start_ms)} - {_format_timestamp(turn.end_ms)}] "
                f"{turn.speaker_name}{confidence}{overlap_mark}"
            )
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Diarization worker admin CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    process_p = sub.add_parser("process-meeting")
    process_p.add_argument("meeting_id")

    show_p = sub.add_parser("show-turns")
    show_p.add_argument("meeting_id")

    args = parser.parse_args()
    meeting_id = uuid.UUID(args.meeting_id)

    if args.command == "process-meeting":
        process_meeting_cmd(meeting_id)
    elif args.command == "show-turns":
        show_turns_cmd(meeting_id)


if __name__ == "__main__":
    main()
