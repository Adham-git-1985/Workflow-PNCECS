"""add indexes used by escalation list and request views

Revision ID: zp5a6b7c8d9e
Revises: zo4a5b6c7d8
Create Date: 2026-10-20 22:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "zp5a6b7c8d9e"
down_revision = "zo4a5b6c7d8"
branch_labels = None
depends_on = None


INDEXES = {
    "request_escalation": (
        (
            "ix_request_escalation_created_id",
            ("created_at", "id"),
        ),
        (
            "ix_request_escalation_request_step_created",
            ("request_id", "step_order", "created_at", "id"),
        ),
    ),
}


def _index_names(bind, inspector, table_name):
    names = {
        index["name"]
        for index in inspector.get_indexes(table_name)
        if index.get("name")
    }
    if bind.dialect.name == "sqlite":
        names.update(
            row[0]
            for row in bind.execute(
                sa.text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'index' AND tbl_name = :table_name"
                ),
                {"table_name": table_name},
            )
            if row[0]
        )
    return names


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table_name, definitions in INDEXES.items():
        if table_name not in inspector.get_table_names():
            continue

        columns = {
            column["name"] for column in inspector.get_columns(table_name)
        }
        existing = _index_names(bind, inspector, table_name)
        for index_name, index_columns in definitions:
            if index_name in existing:
                continue
            if not set(index_columns).issubset(columns):
                continue
            op.create_index(
                index_name,
                table_name,
                list(index_columns),
                unique=False,
            )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table_name, definitions in reversed(tuple(INDEXES.items())):
        if table_name not in inspector.get_table_names():
            continue

        existing = _index_names(bind, inspector, table_name)
        for index_name, _index_columns in reversed(definitions):
            if index_name in existing:
                op.drop_index(index_name, table_name=table_name)
