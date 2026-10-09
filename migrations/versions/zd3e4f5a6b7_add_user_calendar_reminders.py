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


TABLE = "user_calendar_events"
REMINDER_INDEX = "ix_user_calendar_events_reminder"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in inspector.get_table_names():
        return

    columns = {
        column["name"] for column in inspector.get_columns(TABLE)
    }
    definitions = (
        (
            "reminder_minutes_before",
            sa.Column(
                "reminder_minutes_before",
                sa.Integer(),
                nullable=True,
                server_default=sa.text("1440"),
            ),
        ),
        (
            "reminder_sent_at",
            sa.Column("reminder_sent_at", sa.DateTime(), nullable=True),
        ),
        (
            "reminder_sent_for_start_at",
            sa.Column("reminder_sent_for_start_at", sa.DateTime(), nullable=True),
        ),
    )
    for column_name, column in definitions:
        if column_name not in columns:
            op.add_column(TABLE, column)
            columns.add(column_name)

    inspector = sa.inspect(bind)
    indexes = {
        index["name"] for index in inspector.get_indexes(TABLE)
    }
    if (
        REMINDER_INDEX not in indexes
        and {"start_at", "reminder_sent_for_start_at"}.issubset(columns)
    ):
        op.create_index(
            REMINDER_INDEX,
            TABLE,
            ["start_at", "reminder_sent_for_start_at"],
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
    if REMINDER_INDEX in indexes:
        op.drop_index(REMINDER_INDEX, table_name=TABLE)

    columns = {
        column["name"] for column in sa.inspect(bind).get_columns(TABLE)
    }
    for column_name in (
        "reminder_sent_for_start_at",
        "reminder_sent_at",
        "reminder_minutes_before",
    ):
        if column_name in columns:
            op.drop_column(TABLE, column_name)
