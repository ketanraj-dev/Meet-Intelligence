"""Phase 4 revision: per-turn language decision + quality flags

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-14

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("transcript_segments", sa.Column("lid_topk", postgresql.JSONB(), nullable=True))
    op.add_column(
        "transcript_segments", sa.Column("low_confidence", sa.Boolean(), nullable=False, server_default="false")
    )
    op.add_column("transcript_segments", sa.Column("low_confidence_reason", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("transcript_segments", "low_confidence_reason")
    op.drop_column("transcript_segments", "low_confidence")
    op.drop_column("transcript_segments", "lid_topk")
