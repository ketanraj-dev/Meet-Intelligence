"""bot_accounts: last_used_at (LRU rotation) + health check fields

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-13

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("bot_accounts", sa.Column("last_used_at", sa.DateTime(), nullable=True))
    op.add_column("bot_accounts", sa.Column("last_health_check_at", sa.DateTime(), nullable=True))
    op.add_column(
        "bot_accounts",
        sa.Column("needs_relogin", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("bot_accounts", "needs_relogin")
    op.drop_column("bot_accounts", "last_health_check_at")
    op.drop_column("bot_accounts", "last_used_at")
