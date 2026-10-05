"""add attendance-approval inbox indexes

Revision ID: zf5a6b7c8d9e
Revises: ze4f5a6b7c8d
Create Date: 2026-10-05 17:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "zf5a6b7c8d9e"
down_revision = "ze4f5a6b7c8d"
branch_labels = None
depends_on = None


INDEXES = {
    "hr_attendance_schedule_plan": (
        (
            "ix_hr_att_schedule_request_status_updated",
            ("request_type", "status", "updated_at", "id"),
        ),
    ),
    "hr_att_special_case": (
        (
            "ix_hr_att_special_kind_approval_created",
            ("kind", "approval_status", "created_at", "id"),
        ),
    ),
}


def upgrade():
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for table_name, definitions in INDEXES.items():
        if table_name not in tables:
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        existing = {index["name"] for index in inspector.get_indexes(table_name)}
        for index_name, index_columns in definitions:
            if index_name not in existing and set(index_columns).issubset(columns):
                op.create_index(index_name, table_name, list(index_columns), unique=False)


def downgrade():
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for table_name, definitions in reversed(tuple(INDEXES.items())):
        if table_name not in tables:
            continue
        existing = {index["name"] for index in inspector.get_indexes(table_name)}
        for index_name, _columns in reversed(definitions):
            if index_name in existing:
                op.drop_index(index_name, table_name=table_name)
