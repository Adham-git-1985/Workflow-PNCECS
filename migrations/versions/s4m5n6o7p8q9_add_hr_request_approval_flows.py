"""add HR leave/permission approval flows and read-only observers

Revision ID: s4m5n6o7p8q9
Revises: r3l4m5n6o7p8
Create Date: 2026-08-27 15:30:00
"""

from alembic import op
import sqlalchemy as sa


revision = "s4m5n6o7p8q9"
down_revision = "r3l4m5n6o7p8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "hr_request_approval_step",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("request_kind", sa.String(length=20), nullable=False),
        sa.Column("request_id", sa.Integer(), nullable=False),
        sa.Column("step_order", sa.Integer(), nullable=False),
        sa.Column("stage_code", sa.String(length=40), nullable=False),
        sa.Column("approver_scope", sa.String(length=30), nullable=False),
        sa.Column("approver_user_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("assigned_at", sa.DateTime(), nullable=True),
        sa.Column("due_at", sa.DateTime(), nullable=True),
        sa.Column("reminder_sent_at", sa.DateTime(), nullable=True),
        sa.Column("reminder_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("decided_by_id", sa.Integer(), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("escalated_at", sa.DateTime(), nullable=True),
        sa.Column("escalated_from_user_id", sa.Integer(), nullable=True),
        sa.Column("escalation_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("escalation_reason", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["approver_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["decided_by_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["escalated_from_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_kind", "request_id", "step_order", name="uq_hr_req_step_kind_request_order"),
    )
    op.create_index("ix_hr_request_approval_step_request_kind", "hr_request_approval_step", ["request_kind"])
    op.create_index("ix_hr_request_approval_step_request_id", "hr_request_approval_step", ["request_id"])
    op.create_index("ix_hr_request_approval_step_stage_code", "hr_request_approval_step", ["stage_code"])
    op.create_index("ix_hr_request_approval_step_approver_user_id", "hr_request_approval_step", ["approver_user_id"])
    op.create_index("ix_hr_request_approval_step_status", "hr_request_approval_step", ["status"])
    op.create_index("ix_hr_request_approval_step_due_at", "hr_request_approval_step", ["due_at"])
    op.create_index("ix_hr_request_approval_step_decided_by_id", "hr_request_approval_step", ["decided_by_id"])
    op.create_index("ix_hr_req_step_current", "hr_request_approval_step", ["request_kind", "status", "approver_user_id"])

    op.create_table(
        "hr_request_observer",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("request_kind", sa.String(length=20), nullable=False),
        sa.Column("request_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("observer_scope", sa.String(length=40), nullable=False),
        sa.Column("notified_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_kind", "request_id", "user_id", name="uq_hr_req_observer_kind_request_user"),
    )
    op.create_index("ix_hr_request_observer_request_kind", "hr_request_observer", ["request_kind"])
    op.create_index("ix_hr_request_observer_request_id", "hr_request_observer", ["request_id"])
    op.create_index("ix_hr_request_observer_user_id", "hr_request_observer", ["user_id"])
    op.create_index("ix_hr_req_observer_user_kind", "hr_request_observer", ["user_id", "request_kind"])


def downgrade():
    op.drop_index("ix_hr_req_observer_user_kind", table_name="hr_request_observer")
    op.drop_index("ix_hr_request_observer_user_id", table_name="hr_request_observer")
    op.drop_index("ix_hr_request_observer_request_id", table_name="hr_request_observer")
    op.drop_index("ix_hr_request_observer_request_kind", table_name="hr_request_observer")
    op.drop_table("hr_request_observer")

    op.drop_index("ix_hr_req_step_current", table_name="hr_request_approval_step")
    op.drop_index("ix_hr_request_approval_step_decided_by_id", table_name="hr_request_approval_step")
    op.drop_index("ix_hr_request_approval_step_due_at", table_name="hr_request_approval_step")
    op.drop_index("ix_hr_request_approval_step_status", table_name="hr_request_approval_step")
    op.drop_index("ix_hr_request_approval_step_approver_user_id", table_name="hr_request_approval_step")
    op.drop_index("ix_hr_request_approval_step_stage_code", table_name="hr_request_approval_step")
    op.drop_index("ix_hr_request_approval_step_request_id", table_name="hr_request_approval_step")
    op.drop_index("ix_hr_request_approval_step_request_kind", table_name="hr_request_approval_step")
    op.drop_table("hr_request_approval_step")
