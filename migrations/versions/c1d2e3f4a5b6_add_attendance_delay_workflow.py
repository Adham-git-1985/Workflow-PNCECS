"""add workflow-backed attendance delay requests

Revision ID: c1d2e3f4a5b6
Revises: b1c2d3e4f5a6, za1b2c3d4e5f
Create Date: 2026-09-23
"""

from alembic import op
import sqlalchemy as sa


revision = "c1d2e3f4a5b6"
down_revision = ("b1c2d3e4f5a6", "za1b2c3d4e5f")
branch_labels = None
depends_on = None


TABLE = "hr_attendance_delay_request"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table(TABLE):
        return

    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workflow_request_id", sa.Integer(), nullable=False),
        sa.Column("attendance_summary_id", sa.Integer(), nullable=True),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("initiated_by_id", sa.Integer(), nullable=False),
        sa.Column("delay_day", sa.String(length=10), nullable=False),
        sa.Column("late_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("early_leave_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_in_time", sa.String(length=5), nullable=True),
        sa.Column("response_due_at", sa.DateTime(), nullable=True),
        sa.Column("overdue_notified_at", sa.DateTime(), nullable=True),
        sa.Column("employee_responded_at", sa.DateTime(), nullable=True),
        sa.Column("response_payload_json", sa.Text(), nullable=True),
        sa.Column("final_status", sa.String(length=20), nullable=True),
        sa.Column("final_decision_at", sa.DateTime(), nullable=True),
        sa.Column("final_decision_by_id", sa.Integer(), nullable=True),
        sa.Column("notice_archived_file_id", sa.Integer(), nullable=True),
        sa.Column("blank_justification_archived_file_id", sa.Integer(), nullable=True),
        sa.Column("completed_justification_archived_file_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["workflow_request_id"], ["workflow_request.id"]),
        sa.ForeignKeyConstraint(["attendance_summary_id"], ["attendance_daily_summary.id"]),
        sa.ForeignKeyConstraint(["employee_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["initiated_by_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["final_decision_by_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["notice_archived_file_id"], ["archived_file.id"]),
        sa.ForeignKeyConstraint(["blank_justification_archived_file_id"], ["archived_file.id"]),
        sa.ForeignKeyConstraint(["completed_justification_archived_file_id"], ["archived_file.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_request_id", name="uq_hr_attendance_delay_workflow_request"),
        sa.UniqueConstraint("employee_id", "delay_day", name="uq_hr_attendance_delay_employee_day"),
    )
    op.create_index("ix_hr_attendance_delay_request_workflow_request_id", TABLE, ["workflow_request_id"])
    op.create_index("ix_hr_attendance_delay_request_attendance_summary_id", TABLE, ["attendance_summary_id"])
    op.create_index("ix_hr_attendance_delay_request_employee_id", TABLE, ["employee_id"])
    op.create_index("ix_hr_attendance_delay_request_initiated_by_id", TABLE, ["initiated_by_id"])
    op.create_index("ix_hr_attendance_delay_request_delay_day", TABLE, ["delay_day"])
    op.create_index("ix_hr_attendance_delay_request_response_due_at", TABLE, ["response_due_at"])
    op.create_index("ix_hr_attendance_delay_request_final_status", TABLE, ["final_status"])
    op.create_index("ix_hr_attendance_delay_request_created_at", TABLE, ["created_at"])
    op.create_index(
        "ix_hr_attendance_delay_pending_response",
        TABLE,
        ["employee_responded_at", "response_due_at"],
    )


def downgrade():
    bind = op.get_bind()
    if sa.inspect(bind).has_table(TABLE):
        op.drop_table(TABLE)
