"""google calendar trigger + bot account pool

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-10

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("meetings", sa.Column("google_event_id", sa.String(), nullable=True))
    op.add_column("meetings", sa.Column("google_ical_uid", sa.String(), nullable=True))
    op.add_column("meetings", sa.Column("google_etag", sa.String(), nullable=True))
    op.add_column("meetings", sa.Column("owner_email", sa.String(), nullable=True))
    op.create_unique_constraint("uq_meetings_google_ical_uid", "meetings", ["google_ical_uid"])
    op.create_index("ix_meetings_google_event_id", "meetings", ["google_event_id"])

    op.create_table(
        "calendar_sync_state",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("calendar_owner_email", sa.String(), nullable=False),
        sa.Column("calendar_id", sa.String(), nullable=False, server_default="primary"),
        sa.Column("sync_token", sa.Text(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint(
            "calendar_owner_email", "calendar_id", name="uq_calendar_sync_owner_calendar"
        ),
    )

    op.create_table(
        "bot_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(), nullable=False, unique=True),
        sa.Column("profile_path", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="free"),
        sa.Column(
            "current_meeting_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("meetings.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reserved_from", sa.DateTime(), nullable=True),
        sa.Column("reserved_until", sa.DateTime(), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_bot_accounts_status", "bot_accounts", ["status"])


def downgrade() -> None:
    op.drop_index("ix_bot_accounts_status", table_name="bot_accounts")
    op.drop_table("bot_accounts")
    op.drop_table("calendar_sync_state")
    op.drop_index("ix_meetings_google_event_id", table_name="meetings")
    op.drop_constraint("uq_meetings_google_ical_uid", "meetings", type_="unique")
    op.drop_column("meetings", "owner_email")
    op.drop_column("meetings", "google_etag")
    op.drop_column("meetings", "google_ical_uid")
    op.drop_column("meetings", "google_event_id")
