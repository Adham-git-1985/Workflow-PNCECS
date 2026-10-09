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


TABLE = "user_calendar_events"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in inspector.get_table_names():
        op.create_table(
            TABLE,
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

    inspector = sa.inspect(bind)
    columns = {
        column["name"] for column in inspector.get_columns(TABLE)
    }
    indexes = {
        index["name"] for index in inspector.get_indexes(TABLE)
    }
    for index_name, index_columns in (
        (
            "ix_user_calendar_events_owner_start",
            ("owner_user_id", "start_at"),
        ),
        (
            "ix_user_calendar_events_workflow",
            ("workflow_request_id", "workflow_step_order"),
        ),
    ):
        if index_name not in indexes and set(index_columns).issubset(columns):
            op.create_index(
                index_name,
                TABLE,
                list(index_columns),
                unique=False,
            )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in inspector.get_table_names():
        return
    indexes = {
        index["name"] for index in inspector.get_indexes(TABLE)
    }
    for index_name in (
        "ix_user_calendar_events_workflow",
        "ix_user_calendar_events_owner_start",
    ):
        if index_name in indexes:
            op.drop_index(index_name, table_name=TABLE)
    op.drop_table(TABLE)
