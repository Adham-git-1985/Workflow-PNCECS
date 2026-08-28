"""add workflow task email delivery outbox

Revision ID: w8x9y0z1a2b
Revises: v7w8x9y0z1a2
Create Date: 2026-08-28 12:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "w8x9y0z1a2b"
down_revision = "v7w8x9y0z1a2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workflow_task_email_deliveries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.Integer(), nullable=False),
        sa.Column("instance_id", sa.Integer(), nullable=False),
        sa.Column("step_order", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("delivery_kind", sa.String(length=20), nullable=False),
        sa.Column("delivery_date", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("link_url", sa.String(length=500), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["instance_id"], ["workflow_instances.id"]),
        sa.ForeignKeyConstraint(["request_id"], ["workflow_request.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "request_id",
            "instance_id",
            "step_order",
            "user_id",
            "delivery_kind",
            "delivery_date",
            name="uq_workflow_task_email_delivery",
        ),
    )
    op.create_index("ix_workflow_task_email_deliveries_request_id", "workflow_task_email_deliveries", ["request_id"])
    op.create_index("ix_workflow_task_email_deliveries_instance_id", "workflow_task_email_deliveries", ["instance_id"])
    op.create_index("ix_workflow_task_email_deliveries_step_order", "workflow_task_email_deliveries", ["step_order"])
    op.create_index("ix_workflow_task_email_deliveries_user_id", "workflow_task_email_deliveries", ["user_id"])
    op.create_index("ix_workflow_task_email_deliveries_status", "workflow_task_email_deliveries", ["status"])
    op.create_index("ix_workflow_task_email_deliveries_next_attempt_at", "workflow_task_email_deliveries", ["next_attempt_at"])
    op.create_index(
        "ix_workflow_task_email_delivery_pending",
        "workflow_task_email_deliveries",
        ["status", "next_attempt_at", "created_at"],
    )


def downgrade():
    op.drop_index("ix_workflow_task_email_delivery_pending", table_name="workflow_task_email_deliveries")
    op.drop_index("ix_workflow_task_email_deliveries_next_attempt_at", table_name="workflow_task_email_deliveries")
    op.drop_index("ix_workflow_task_email_deliveries_status", table_name="workflow_task_email_deliveries")
    op.drop_index("ix_workflow_task_email_deliveries_user_id", table_name="workflow_task_email_deliveries")
    op.drop_index("ix_workflow_task_email_deliveries_step_order", table_name="workflow_task_email_deliveries")
    op.drop_index("ix_workflow_task_email_deliveries_instance_id", table_name="workflow_task_email_deliveries")
    op.drop_index("ix_workflow_task_email_deliveries_request_id", table_name="workflow_task_email_deliveries")
    op.drop_table("workflow_task_email_deliveries")
