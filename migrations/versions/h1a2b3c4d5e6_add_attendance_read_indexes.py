"""add indexes for daily attendance and departure reads

Revision ID: h1a2b3c4d5e6
Revises: f9a0b1c2d3e4
Create Date: 2026-09-13
"""

from alembic import op
import sqlalchemy as sa


revision = "h1a2b3c4d5e6"
down_revision = "f9a0b1c2d3e4"
branch_labels = None
depends_on = None


INDEXES = {
    "attendance_daily_summary": (
        ("ix_att_daily_day_status", ("day", "status")),
    ),
    "attendance_event": (
        ("ix_att_event_user_dt_type", ("user_id", "event_dt", "event_type")),
    ),
    "hr_permission_request": (
        ("ix_hr_perm_req_status_day_user", ("status", "day", "user_id")),
    ),
}


def upgrade():
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for table_name, definitions in INDEXES.items():
        if table_name not in tables:
            continue
        existing = {index["name"] for index in inspector.get_indexes(table_name)}
        for index_name, columns in definitions:
            if index_name not in existing:
                op.create_index(index_name, table_name, list(columns), unique=False)


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
