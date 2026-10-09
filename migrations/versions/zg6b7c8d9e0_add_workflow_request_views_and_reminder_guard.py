"""add workflow request views and followup reminder uniqueness

Revision ID: zg6b7c8d9e0
Revises: zf5a6b7c8d9e
Create Date: 2026-10-09 12:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "zg6b7c8d9e0"
down_revision = "zf5a6b7c8d9e"
branch_labels = None
depends_on = None


VIEW_TABLE = "workflow_request_views"
REMINDER_INDEX = "uq_notification_followup_reminder_event"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if VIEW_TABLE not in tables:
        op.create_table(
            VIEW_TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "request_id",
                sa.Integer(),
                sa.ForeignKey("workflow_request.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("step_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("viewed_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "user_id",
                "request_id",
                name="uq_workflow_request_view_user_request",
            ),
        )

    inspector = sa.inspect(bind)
    view_indexes = {
        index["name"] for index in inspector.get_indexes(VIEW_TABLE)
    }
    for index_name, columns in (
        ("ix_workflow_request_views_user_id", ["user_id"]),
        ("ix_workflow_request_views_request_id", ["request_id"]),
        ("ix_workflow_request_views_user_step", ["user_id", "step_order"]),
    ):
        if index_name not in view_indexes:
            op.create_index(index_name, VIEW_TABLE, columns, unique=False)

    inspector = sa.inspect(bind)
    if "notification" not in inspector.get_table_names():
        return
    notification_indexes = {
        index["name"] for index in inspector.get_indexes("notification")
    }
    notification_columns = {
        column["name"] for column in inspector.get_columns("notification")
    }
    dialect = bind.dialect.name
    if (
        REMINDER_INDEX not in notification_indexes
        and {"user_id", "event_key", "is_mirror"}.issubset(notification_columns)
        and dialect in {"sqlite", "postgresql"}
    ):
        where_clause = sa.text("event_key LIKE 'followup-reminder:%'")
        dialect_options = {f"{dialect}_where": where_clause}
        op.create_index(
            REMINDER_INDEX,
            "notification",
            ["user_id", "event_key", "is_mirror"],
            unique=True,
            **dialect_options,
        )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "notification" in tables:
        notification_indexes = {
            index["name"] for index in inspector.get_indexes("notification")
        }
        if REMINDER_INDEX in notification_indexes:
            op.drop_index(REMINDER_INDEX, table_name="notification")
    if VIEW_TABLE in tables:
        op.drop_table(VIEW_TABLE)
