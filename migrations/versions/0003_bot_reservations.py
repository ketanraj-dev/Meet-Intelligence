"""bot reservations table (replaces reserved_from/until) + participant email

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-12

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import ExcludeConstraint

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Needed for the GiST exclusion constraint's "=" operator class on a uuid column.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.add_column("participants", sa.Column("email", sa.String(), nullable=True))

    op.create_table(
        "bot_reservations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "bot_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bot_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "meeting_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("meetings.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("window", postgresql.TSTZRANGE(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        ExcludeConstraint(
            ("bot_account_id", "="),
            ("window", "&&"),
            using="gist",
            name="ex_bot_reservations_no_overlap",
        ),
    )
    op.create_index("ix_bot_reservations_bot_account_id", "bot_reservations", ["bot_account_id"])

    # No DB-level CHECK ever enforced bot_accounts.status values (it's a plain
    # String), so there's nothing to migrate for the removed 'reserved' value
    # beyond the application-level enum change in common/models.py.
    op.drop_column("bot_accounts", "reserved_from")
    op.drop_column("bot_accounts", "reserved_until")


def downgrade() -> None:
    op.add_column("bot_accounts", sa.Column("reserved_from", sa.DateTime(), nullable=True))
    op.add_column("bot_accounts", sa.Column("reserved_until", sa.DateTime(), nullable=True))
    op.drop_index("ix_bot_reservations_bot_account_id", table_name="bot_reservations")
    op.drop_table("bot_reservations")
    op.drop_column("participants", "email")
