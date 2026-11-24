"""add the lightweight attendance-delay notice state

Revision ID: zy4d5e6f7a8b
Revises: zx3c4d5e6f7a
Create Date: 2026-11-24 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "zy4d5e6f7a8b"
down_revision = "zx3c4d5e6f7a"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    table = "hr_attendance_delay_request"
    if table not in tables:
        return

    columns = {column["name"] for column in sa.inspect(bind).get_columns(table)}
    additions = (
        ("case_status", sa.Column("case_status", sa.String(length=32), nullable=False, server_default="PENDING_EMPLOYEE")),
        ("current_assignee_id", sa.Column("current_assignee_id", sa.Integer(), nullable=True)),
        ("secretary_general_note", sa.Column("secretary_general_note", sa.Text(), nullable=True)),
        ("secretary_general_action_at", sa.Column("secretary_general_action_at", sa.DateTime(), nullable=True)),
        ("secretary_general_action_by_id", sa.Column("secretary_general_action_by_id", sa.Integer(), nullable=True)),
        ("administrative_action_note", sa.Column("administrative_action_note", sa.Text(), nullable=True)),
        ("administrative_action_at", sa.Column("administrative_action_at", sa.DateTime(), nullable=True)),
        ("administrative_action_by_id", sa.Column("administrative_action_by_id", sa.Integer(), nullable=True)),
    )
    for name, column in additions:
        if name not in columns:
            op.add_column(table, column)

    indexes = {index["name"] for index in sa.inspect(bind).get_indexes(table)}
    if "ix_hr_attendance_delay_case_status_assignee" not in indexes:
        op.create_index(
            "ix_hr_attendance_delay_case_status_assignee",
            table,
            ["case_status", "current_assignee_id"],
        )


def downgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    table = "hr_attendance_delay_request"
    if table not in tables:
        return
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes(table)}
    if "ix_hr_attendance_delay_case_status_assignee" in indexes:
        op.drop_index("ix_hr_attendance_delay_case_status_assignee", table_name=table)
    columns = {column["name"] for column in sa.inspect(bind).get_columns(table)}
    for name in (
        "administrative_action_by_id",
        "administrative_action_at",
        "administrative_action_note",
        "secretary_general_action_by_id",
        "secretary_general_action_at",
        "secretary_general_note",
        "current_assignee_id",
        "case_status",
    ):
        if name in columns:
            op.drop_column(table, name)
