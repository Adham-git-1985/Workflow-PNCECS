"""add scheduled HR attendance report email deliveries

Revision ID: z9a0b1c2d3e4
Revises: z7a8b9c0d1e2
Create Date: 2026-09-23
"""

from alembic import op
import sqlalchemy as sa


revision = "z9a0b1c2d3e4"
down_revision = "z7a8b9c0d1e2"
branch_labels = None
depends_on = None


TABLE = "hr_attendance_email_report_delivery"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table(TABLE):
        return

    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_date", sa.String(length=10), nullable=False),
        sa.Column("report_day", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("recipient_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_date", name="uq_hr_attendance_email_report_delivery_run_date"),
    )
    op.create_index("ix_hr_attendance_email_report_delivery_run_date", TABLE, ["run_date"])
    op.create_index("ix_hr_attendance_email_report_delivery_report_day", TABLE, ["report_day"])
    op.create_index("ix_hr_attendance_email_report_delivery_status", TABLE, ["status"])
    op.create_index("ix_hr_attendance_email_delivery_status_date", TABLE, ["status", "run_date"])
    op.create_index("ix_hr_attendance_email_report_delivery_created_at", TABLE, ["created_at"])


def downgrade():
    bind = op.get_bind()
    if sa.inspect(bind).has_table(TABLE):
        op.drop_table(TABLE)
