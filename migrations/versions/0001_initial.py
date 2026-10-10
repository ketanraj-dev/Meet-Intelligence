"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-10

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "meetings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("meet_url", sa.String(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="scheduled"),
        sa.Column("expected_languages", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("audio_key", sa.String(), nullable=True),
        sa.Column("speaker_events_key", sa.String(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_meetings_status", "meetings", ["status"])
    op.create_index("ix_meetings_scheduled_at", "meetings", ["scheduled_at"])

    op.create_table(
        "participants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "meeting_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("meetings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False, server_default="unknown"),
    )
    op.create_index("ix_participants_meeting_id", "participants", ["meeting_id"])

    op.create_table(
        "transcript_segments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "meeting_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("meetings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "participant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("participants.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("language", sa.String(), nullable=True),
        sa.Column("language_source", sa.String(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_en", sa.Text(), nullable=True),
        sa.Column("asr_model", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_transcript_segments_meeting_id", "transcript_segments", ["meeting_id"]
    )

    op.create_table(
        "segment_sentiment",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "segment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("transcript_segments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("model_version", sa.String(), nullable=False),
    )
    op.create_index(
        "ix_segment_sentiment_segment_id", "segment_sentiment", ["segment_id"]
    )

    op.create_table(
        "meeting_analysis",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "meeting_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("meetings.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("action_items", postgresql.JSONB(), nullable=True),
        sa.Column("key_topics", postgresql.JSONB(), nullable=True),
        sa.Column("objections", postgresql.JSONB(), nullable=True),
        sa.Column("overall_sentiment", sa.String(), nullable=True),
        sa.Column("sentiment_by_speaker", postgresql.JSONB(), nullable=True),
        sa.Column("sentiment_trajectory", postgresql.JSONB(), nullable=True),
        sa.Column("outcome_score", sa.Float(), nullable=True),
        sa.Column("model_version", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("meeting_analysis")
    op.drop_index("ix_segment_sentiment_segment_id", table_name="segment_sentiment")
    op.drop_table("segment_sentiment")
    op.drop_index("ix_transcript_segments_meeting_id", table_name="transcript_segments")
    op.drop_table("transcript_segments")
    op.drop_index("ix_participants_meeting_id", table_name="participants")
    op.drop_table("participants")
    op.drop_index("ix_meetings_scheduled_at", table_name="meetings")
    op.drop_index("ix_meetings_status", table_name="meetings")
    op.drop_table("meetings")
