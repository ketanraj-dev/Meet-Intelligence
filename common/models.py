"""SQLAlchemy ORM models for the full schema (see project spec).

Phase 1 only exposes the `meetings` table through the API, but all tables
are created now from the agreed-upon schema so later phases don't need
structural migrations, just new code that reads/writes them.
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSTZRANGE, UUID, ExcludeConstraint, Range
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class MeetingStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    JOINING = "joining"
    IN_CALL = "in_call"
    RECORDED = "recorded"
    DIARIZING = "diarizing"
    DIARIZED = "diarized"
    TRANSCRIBING = "transcribing"
    TRANSCRIBED = "transcribed"
    ANALYZING = "analyzing"
    DONE = "done"
    FAILED = "failed"
    BOT_REJECTED = "bot_rejected"
    NO_BOT_AVAILABLE = "no_bot_available"
    CANCELLED = "cancelled"


class ParticipantRole(str, enum.Enum):
    HOST = "host"
    CLIENT = "client"
    AGENT = "agent"
    UNKNOWN = "unknown"


class BotAccountStatus(str, enum.Enum):
    """Live state only. Future bookings live in BotReservation, independent of
    this — an account can be `free` right now and still hold reservations for
    later, non-overlapping meetings."""

    FREE = "free"
    IN_USE = "in_use"
    DISABLED = "disabled"


class Meeting(Base):
    __tablename__ = "meetings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    meet_url: Mapped[str] = mapped_column(String, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(nullable=True)
    status: Mapped[MeetingStatus] = mapped_column(
        String, nullable=False, default=MeetingStatus.SCHEDULED
    )
    expected_languages: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    audio_key: Mapped[str | None] = mapped_column(String, nullable=True)
    speaker_events_key: Mapped[str | None] = mapped_column(String, nullable=True)
    # Set by the bot's end-of-call self-check when the recorded audio showed
    # real speech but zero speaking_start events were ever logged — a strong
    # sign selectors.yaml's speaking_indicator stopped matching (e.g. a Meet
    # UI change), silently producing empty-but-valid-looking speaker data.
    speaking_indicator_suspect: Mapped[bool] = mapped_column(nullable=False, default=False)
    # Counts failed bot_launcher launch attempts (container never actually
    # started — e.g. a bad docker-py call, Docker daemon unreachable). A
    # failed launch reverts status to `scheduled` so the next poll retries,
    # up to bot_launcher.MAX_LAUNCH_ATTEMPTS, then gives up and sets
    # `failed`. Never incremented by the bot container itself — only by the
    # scheduler's launch path, before a container exists at all.
    launch_attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    # Counts failed worker/diarization.py process_meeting() attempts for
    # this meeting (download/diarize/write failures — not a disagreement
    # about content). worker/main.py reverts status to `recorded` and
    # retries up to MAX_DIARIZATION_ATTEMPTS, then gives up and sets
    # `failed` — same retry convention as launch_attempts above.
    diarization_attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    # Set when speakers.jsonl had zero speaking_start/stop events (e.g. a
    # broken speaking_indicator selector, see Phase 2's
    # speaking_indicator_suspect) — diarization still runs, but clusters
    # get generic "Speaker A"/"Speaker B" labels instead of real names,
    # since there's no timing data to map them against.
    diarization_name_mapping_suspect: Mapped[bool] = mapped_column(nullable=False, default=False)
    # Same retry convention as diarization_attempts, for
    # asr/transcription.py's transcribe_meeting() attempts.
    transcription_attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    google_event_id: Mapped[str | None] = mapped_column(String, nullable=True)
    google_ical_uid: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    google_etag: Mapped[str | None] = mapped_column(String, nullable=True)
    owner_email: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    participants: Mapped[list["Participant"]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan"
    )
    speaker_turns: Mapped[list["SpeakerTurn"]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", order_by="SpeakerTurn.start_ms"
    )
    transcript_segments: Mapped[list["TranscriptSegment"]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan"
    )
    analysis: Mapped["MeetingAnalysis | None"] = relationship(
        back_populates="meeting", cascade="all, delete-orphan"
    )


class Participant(Base):
    __tablename__ = "participants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False
    )
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    role: Mapped[ParticipantRole] = mapped_column(
        String, nullable=False, default=ParticipantRole.UNKNOWN
    )

    meeting: Mapped["Meeting"] = relationship(back_populates="participants")


class SpeakerTurn(Base):
    """One pyannote-diarized speaker turn (Phase 3), ready for Phase 4 to
    transcribe. Idempotent per meeting — worker/diarization.py deletes and
    fully replaces a meeting's turns on every (re-)run, never appends.

    participant_id is NOT a foreign key to `participants` — that table
    holds calendar-invite identities (host/guest email, from
    calendar_sync), a different identity system with no guaranteed 1:1
    correspondence to who's actually on a given Meet tile. This instead
    stores the Meet-session participant id straight from speakers.jsonl
    (e.g. "spaces/.../devices/437"), which is what the overlap-based name
    mapping actually has timing data for. NULL when unmapped (an
    "Unknown speaker N" cluster, or the whole meeting fell back to
    "Speaker A"/"Speaker B" naming — see Meeting.diarization_name_mapping_suspect).
    """

    __tablename__ = "speaker_turns"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False
    )
    cluster_label: Mapped[str] = mapped_column(String, nullable=False)
    participant_id: Mapped[str | None] = mapped_column(String, nullable=True)
    speaker_name: Mapped[str] = mapped_column(String, nullable=False)
    mapping_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    overlap: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    meeting: Mapped["Meeting"] = relationship(back_populates="speaker_turns")


class TranscriptSegment(Base):
    """Phase 4: one row per speaker_turns row, transcribed. 1:1 with
    SpeakerTurn (speaker_turn_id, unique) — that's the real link; the
    pre-existing participant_id (FK to `participants`, the calendar-invite
    identity table) stays nullable/unpopulated for now, same reasoning as
    SpeakerTurn.participant_id's own docstring: no reliable 1:1
    correspondence between a Meet-tile speaker and a calendar invitee.
    show-transcript resolves a display role via a best-effort name match
    instead of this FK."""

    __tablename__ = "transcript_segments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False
    )
    speaker_turn_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("speaker_turns.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    participant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("participants.id", ondelete="SET NULL"), nullable=True
    )
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    # Per-TURN language ID (faster-whisper), top-1 result before any
    # en-confidence-gate decision — `language` below is what was actually
    # USED to transcribe, which can differ (e.g. detected "ta", used "hi"
    # because expected_languages didn't include Tamil). See
    # asr/transcription.py's decide_turn_language.
    detected_language: Mapped[str | None] = mapped_column(String, nullable=True)
    # Full LID distribution, best-first: [[lang, probability], ...]. None
    # for turns skipped as silence/noise (LID never ran).
    lid_topk: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    language: Mapped[str | None] = mapped_column(String, nullable=True)
    # Why `language` was chosen: "lid_en_high_confidence" |
    # "default_indic_mixed_mode" | "restricted_to_expected_languages" |
    # "skipped_silence_or_noise".
    language_source: Mapped[str | None] = mapped_column(String, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    asr_model: Mapped[str | None] = mapped_column(String, nullable=True)
    # Flagged when: the ASR repetition guard had to intervene (loop
    # detected, even if successfully split-and-recovered), the final text's
    # word rate is wildly disproportionate to the turn's duration, or the
    # turn was skipped as silence/noise. NOT a hard quality gate — a
    # pointer for a human (or a later phase) to double-check this segment.
    low_confidence: Mapped[bool] = mapped_column(nullable=False, default=False)
    low_confidence_reason: Mapped[str | None] = mapped_column(String, nullable=True)

    meeting: Mapped["Meeting"] = relationship(back_populates="transcript_segments")
    speaker_turn: Mapped["SpeakerTurn"] = relationship()
    participant: Mapped["Participant | None"] = relationship()
    sentiment: Mapped["SegmentSentiment | None"] = relationship(
        back_populates="segment", cascade="all, delete-orphan"
    )


class SegmentSentiment(Base):
    __tablename__ = "segment_sentiment"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    segment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transcript_segments.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    model_version: Mapped[str] = mapped_column(String, nullable=False)

    segment: Mapped["TranscriptSegment"] = relationship(back_populates="sentiment")


class MeetingAnalysis(Base):
    __tablename__ = "meeting_analysis"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    action_items: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    key_topics: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    objections: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    overall_sentiment: Mapped[str | None] = mapped_column(String, nullable=True)
    sentiment_by_speaker: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    sentiment_trajectory: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    outcome_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_version: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    meeting: Mapped["Meeting"] = relationship(back_populates="analysis")


class CalendarSyncState(Base):
    """Per-(employee calendar) incremental sync cursor for the Google Calendar poller."""

    __tablename__ = "calendar_sync_state"
    __table_args__ = (
        UniqueConstraint("calendar_owner_email", "calendar_id", name="uq_calendar_sync_owner_calendar"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    calendar_owner_email: Mapped[str] = mapped_column(String, nullable=False)
    calendar_id: Mapped[str] = mapped_column(String, nullable=False, default="primary")
    sync_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)


class BotAccount(Base):
    """A Gmail account + persistent Chrome profile the meeting bot can run as.

    Exactly one running bot container may use a given account at a time
    (status='in_use'); see bot_pool/service.py for checkout/release/reserve.
    """

    __tablename__ = "bot_accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    profile_path: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[BotAccountStatus] = mapped_column(
        String, nullable=False, default=BotAccountStatus.FREE
    )
    current_meeting_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meetings.id", ondelete="SET NULL"), nullable=True
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # Set at checkout (not reservation, not release) — the pool's selection
    # queries order by this ascending (nulls first) to rotate through every
    # account rather than always preferring the same one, so every
    # account's Google session gets exercised regularly and doesn't
    # silently expire from disuse. See bot_pool/service.py.
    last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # Written by the weekly health check (scheduler/health_check.py, a
    # one-shot BOT_MODE=health_check container per idle account) — whether
    # its saved Google session still appears to be signed in.
    last_health_check_at: Mapped[datetime | None] = mapped_column(nullable=True)
    needs_relogin: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)


class BotReservation(Base):
    """A future booking of a bot account for one meeting's time window.

    An account may hold many BotReservation rows as long as their windows
    don't overlap — enforced by the EXCLUDE constraint below at the DB level,
    not just in application code, so a race between two schedulers can never
    double-book the same account for overlapping meetings.
    """

    __tablename__ = "bot_reservations"
    __table_args__ = (
        ExcludeConstraint(
            ("bot_account_id", "="),
            ("window", "&&"),
            using="gist",
            name="ex_bot_reservations_no_overlap",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bot_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bot_accounts.id", ondelete="CASCADE"), nullable=False
    )
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    window: Mapped[Range] = mapped_column(TSTZRANGE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    bot_account: Mapped["BotAccount"] = relationship()
    meeting: Mapped["Meeting"] = relationship()
