"""add departure reapproval history and approval-round grouping

Revision ID: a3b4c5d6e7f8
Revises: z1a2b3c4d5e6
Create Date: 2026-09-01
"""

from alembic import op
import sqlalchemy as sa


revision = "a3b4c5d6e7f8"
down_revision = "z1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("hr_request_approval_step") as batch_op:
        batch_op.add_column(
            sa.Column("flow_revision", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.create_index(
            "ix_hr_request_approval_step_flow_revision",
            ["flow_revision"],
            unique=False,
        )

    op.create_table(
        "hr_permission_request_revision",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.Integer(), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("previous_permission_type_name", sa.String(length=255), nullable=True),
        sa.Column("current_permission_type_name", sa.String(length=255), nullable=True),
        sa.Column("previous_day", sa.String(length=10), nullable=True),
        sa.Column("current_day", sa.String(length=10), nullable=True),
        sa.Column("previous_from_time", sa.String(length=5), nullable=True),
        sa.Column("current_from_time", sa.String(length=5), nullable=True),
        sa.Column("previous_to_time", sa.String(length=5), nullable=True),
        sa.Column("current_to_time", sa.String(length=5), nullable=True),
        sa.Column("previous_note", sa.Text(), nullable=True),
        sa.Column("current_note", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("resubmitted_by_id", sa.Integer(), nullable=False),
        sa.Column("resubmitted_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["request_id"], ["hr_permission_request.id"]),
        sa.ForeignKeyConstraint(["resubmitted_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", "revision_no", name="uq_hr_perm_revision_request_number"),
    )
    op.create_index(
        "ix_hr_permission_request_revision_request_id",
        "hr_permission_request_revision",
        ["request_id"],
        unique=False,
    )
    op.create_index(
        "ix_hr_permission_request_revision_resubmitted_by_id",
        "hr_permission_request_revision",
        ["resubmitted_by_id"],
        unique=False,
    )
    op.create_index(
        "ix_hr_perm_revision_request_submitted",
        "hr_permission_request_revision",
        ["request_id", "resubmitted_at"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_hr_perm_revision_request_submitted", table_name="hr_permission_request_revision")
    op.drop_index("ix_hr_permission_request_revision_resubmitted_by_id", table_name="hr_permission_request_revision")
    op.drop_index("ix_hr_permission_request_revision_request_id", table_name="hr_permission_request_revision")
    op.drop_table("hr_permission_request_revision")
    with op.batch_alter_table("hr_request_approval_step") as batch_op:
        batch_op.drop_index("ix_hr_request_approval_step_flow_revision")
        batch_op.drop_column("flow_revision")
