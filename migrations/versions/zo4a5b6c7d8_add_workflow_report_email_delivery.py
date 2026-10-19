"""add Masar workflow report email delivery history

Revision ID: zo4a5b6c7d8
Revises: zn3a4b5c6d7e
Create Date: 2026-10-19 12:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "zo4a5b6c7d8"
down_revision = "zn3a4b5c6d7e"
branch_labels = None
depends_on = None


TABLE = "workflow_report_email_delivery"


def upgrade():
    bind = op.get_bind()
    if sa.inspect(bind).has_table(TABLE):
        return

    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("report_key", sa.String(length=80), nullable=False, server_default="EMPLOYEE_WORKFLOW"),
        sa.Column("slot_key", sa.String(length=120), nullable=False),
        sa.Column("run_date", sa.String(length=10), nullable=False),
        sa.Column("scheduled_time", sa.String(length=20), nullable=False, server_default="10:00"),
        sa.Column("trigger", sa.String(length=20), nullable=False, server_default="SCHEDULED"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("recipient_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("employee_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("assignment_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("slot_key", name="uq_workflow_report_email_delivery_slot"),
    )
    op.create_index(
        "ix_workflow_report_email_delivery_report_key",
        TABLE,
        ["report_key"],
    )
    op.create_index(
        "ix_workflow_report_email_delivery_run_date",
        TABLE,
        ["run_date"],
    )
    op.create_index(
        "ix_workflow_report_email_delivery_scheduled_time",
        TABLE,
        ["scheduled_time"],
    )
    op.create_index(
        "ix_workflow_report_email_delivery_trigger",
        TABLE,
        ["trigger"],
    )
    op.create_index(
        "ix_workflow_report_email_delivery_status",
        TABLE,
        ["status"],
    )
    op.create_index(
        "ix_workflow_report_email_delivery_created_at",
        TABLE,
        ["created_at"],
    )
    op.create_index(
        "ix_workflow_report_email_delivery_report_date",
        TABLE,
        ["report_key", "run_date", "scheduled_time"],
    )
    op.create_index(
        "ix_workflow_report_email_delivery_status_date",
        TABLE,
        ["status", "run_date"],
    )


def downgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return

    op.drop_index("ix_workflow_report_email_delivery_status_date", table_name=TABLE)
    op.drop_index("ix_workflow_report_email_delivery_report_date", table_name=TABLE)
    op.drop_index("ix_workflow_report_email_delivery_created_at", table_name=TABLE)
    op.drop_index("ix_workflow_report_email_delivery_status", table_name=TABLE)
    op.drop_index("ix_workflow_report_email_delivery_trigger", table_name=TABLE)
    op.drop_index("ix_workflow_report_email_delivery_scheduled_time", table_name=TABLE)
    op.drop_index("ix_workflow_report_email_delivery_run_date", table_name=TABLE)
    op.drop_index("ix_workflow_report_email_delivery_report_key", table_name=TABLE)
    op.drop_table(TABLE)
