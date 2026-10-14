"""speaker_turns table (Phase 3 diarization) + meetings diarization fields

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-13

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("meetings", sa.Column("diarization_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column(
        "meetings",
        sa.Column("diarization_name_mapping_suspect", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.create_table(
        "speaker_turns",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "meeting_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("meetings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("cluster_label", sa.String(), nullable=False),
        sa.Column("participant_id", sa.String(), nullable=True),
        sa.Column("speaker_name", sa.String(), nullable=False),
        sa.Column("mapping_confidence", sa.Float(), nullable=True),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("overlap", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_speaker_turns_meeting_id", "speaker_turns", ["meeting_id"])


def downgrade() -> None:
    op.drop_index("ix_speaker_turns_meeting_id", table_name="speaker_turns")
    op.drop_table("speaker_turns")
    op.drop_column("meetings", "diarization_name_mapping_suspect")
    op.drop_column("meetings", "diarization_attempts")
