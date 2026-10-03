"""add user calendar reminder delivery fields

Revision ID: zd3e4f5a6b7
Revises: zc3d4e5f6a7
Create Date: 2026-10-03
"""

from alembic import op
import sqlalchemy as sa


revision = "zd3e4f5a6b7"
down_revision = "zc3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "user_calendar_events",
        sa.Column(
            "reminder_minutes_before",
            sa.Integer(),
            nullable=True,
            server_default=sa.text("1440"),
        ),
    )
    op.add_column(
        "user_calendar_events",
        sa.Column("reminder_sent_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "user_calendar_events",
        sa.Column("reminder_sent_for_start_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_user_calendar_events_reminder",
        "user_calendar_events",
        ["start_at", "reminder_sent_for_start_at"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_user_calendar_events_reminder", table_name="user_calendar_events")
    op.drop_column("user_calendar_events", "reminder_sent_for_start_at")
    op.drop_column("user_calendar_events", "reminder_sent_at")
    op.drop_column("user_calendar_events", "reminder_minutes_before")
