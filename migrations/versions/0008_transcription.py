"""Phase 4: transcription_attempts + transcript_segments links

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-13

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("meetings", sa.Column("transcription_attempts", sa.Integer(), nullable=False, server_default="0"))

    op.add_column(
        "transcript_segments",
        sa.Column(
            "speaker_turn_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("speaker_turns.id", ondelete="CASCADE"),
            nullable=True,  # backfilled below, then tightened to NOT NULL
        ),
    )
    op.add_column("transcript_segments", sa.Column("detected_language", sa.String(), nullable=True))
    # No pre-existing rows in practice (Phase 4 is what starts writing this
    # table), but tighten correctly either way rather than leaving a NULL
    # loophole for new rows.
    op.alter_column("transcript_segments", "speaker_turn_id", nullable=False)
    op.create_unique_constraint("uq_transcript_segments_speaker_turn_id", "transcript_segments", ["speaker_turn_id"])


def downgrade() -> None:
    op.drop_constraint("uq_transcript_segments_speaker_turn_id", "transcript_segments", type_="unique")
    op.drop_column("transcript_segments", "detected_language")
    op.drop_column("transcript_segments", "speaker_turn_id")
    op.drop_column("meetings", "transcription_attempts")
