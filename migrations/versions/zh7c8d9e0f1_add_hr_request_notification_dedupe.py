"""deduplicate HR request notification events

Revision ID: zh7c8d9e0f1
Revises: zg6b7c8d9e0
Create Date: 2026-10-09 22:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "zh7c8d9e0f1"
down_revision = "zg6b7c8d9e0"
branch_labels = None
depends_on = None


INDEX_NAME = "uq_notification_hr_request_event"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "notification" not in inspector.get_table_names():
        return

    indexes = {
        index["name"] for index in inspector.get_indexes("notification")
    }
    columns = {
        column["name"] for column in inspector.get_columns("notification")
    }
    dialect = bind.dialect.name
    if (
        INDEX_NAME not in indexes
        and {"user_id", "event_key", "is_mirror"}.issubset(columns)
        and dialect in {"sqlite", "postgresql"}
    ):
        where_clause = sa.text("event_key LIKE 'hr-request:%'")
        op.create_index(
            INDEX_NAME,
            "notification",
            ["user_id", "event_key", "is_mirror"],
            unique=True,
            **{f"{dialect}_where": where_clause},
        )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "notification" not in inspector.get_table_names():
        return
    indexes = {
        index["name"] for index in inspector.get_indexes("notification")
    }
    if INDEX_NAME in indexes:
        op.drop_index(INDEX_NAME, table_name="notification")
