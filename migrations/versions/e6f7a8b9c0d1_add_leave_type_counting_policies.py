"""add configurable balance and duration policies to leave types

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c
Create Date: 2026-09-03
"""

from alembic import op
import sqlalchemy as sa


revision = "e6f7a8b9c0d1"
down_revision = "d5e6f7a8b9c"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "hr_leave_type",
        sa.Column("deduct_from_balance", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "hr_leave_type",
        sa.Column(
            "day_count_basis",
            sa.String(length=20),
            nullable=False,
            server_default="WORKING_DAYS",
        ),
    )
    op.add_column(
        "hr_leave_type",
        sa.Column(
            "exclude_official_holidays",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.execute(
        sa.text(
            "UPDATE hr_leave_type SET deduct_from_balance = 0 "
            "WHERE UPPER(code) IN ('M', 'MATERNITY', 'PATERNITY', 'P', 'PATERNITY_LEAVE')"
        )
    )
    op.execute(
        sa.text(
            "UPDATE hr_leave_type SET day_count_basis = 'CALENDAR_DAYS' "
            "WHERE UPPER(code) IN ('M', 'MATERNITY')"
        )
    )
    op.alter_column("hr_leave_type", "deduct_from_balance", server_default=None)
    op.alter_column("hr_leave_type", "day_count_basis", server_default=None)
    op.alter_column("hr_leave_type", "exclude_official_holidays", server_default=None)


def downgrade():
    op.drop_column("hr_leave_type", "exclude_official_holidays")
    op.drop_column("hr_leave_type", "day_count_basis")
    op.drop_column("hr_leave_type", "deduct_from_balance")
