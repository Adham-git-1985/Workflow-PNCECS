"""add private user calendar events

Revision ID: zc3d4e5f6a7
Revises: c1d2e3f4a5b6, zb2c3d4e5f6
Create Date: 2026-10-03
"""

from alembic import op
import sqlalchemy as sa


revision = "zc3d4e5f6a7"
down_revision = ("c1d2e3f4a5b6", "zb2c3d4e5f6")
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_calendar_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("event_type", sa.String(length=30), nullable=False, server_default="PERSONAL"),
        sa.Column("start_at", sa.DateTime(), nullable=False),
        sa.Column("end_at", sa.DateTime(), nullable=True),
        sa.Column("all_day", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("workflow_request_id", sa.Integer(), nullable=True),
        sa.Column("workflow_step_order", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workflow_request_id"], ["workflow_request.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_user_calendar_events_owner_start",
        "user_calendar_events",
        ["owner_user_id", "start_at"],
        unique=False,
    )
    op.create_index(
        "ix_user_calendar_events_workflow",
        "user_calendar_events",
        ["workflow_request_id", "workflow_step_order"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_user_calendar_events_workflow", table_name="user_calendar_events")
    op.drop_index("ix_user_calendar_events_owner_start", table_name="user_calendar_events")
    op.drop_table("user_calendar_events")
