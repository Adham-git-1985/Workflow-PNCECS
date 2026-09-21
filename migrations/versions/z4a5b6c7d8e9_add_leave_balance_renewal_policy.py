"""add leave-balance renewal policies

Revision ID: z4a5b6c7d8e9
Revises: z3a4b5c6d7e8
Create Date: 2026-09-21
"""

from alembic import op
import sqlalchemy as sa


revision = "z4a5b6c7d8e9"
down_revision = "z3a4b5c6d7e8"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("hr_leave_type"):
        return

    columns = {column["name"] for column in inspector.get_columns("hr_leave_type")}
    if "balance_renewal_policy" not in columns:
        op.add_column(
            "hr_leave_type",
            sa.Column(
                "balance_renewal_policy",
                sa.String(length=20),
                nullable=False,
                server_default="YEARLY",
            ),
        )
        columns.add("balance_renewal_policy")

    # The new business rule makes these balances renewable at the beginning
    # of each year.  It applies during this one-time upgrade only, leaving any
    # later administrative change intact.
    op.execute(sa.text(
        "UPDATE hr_leave_type "
        "SET balance_renewal_policy = 'YEARLY', deduct_from_balance = 1 "
        "WHERE UPPER(code) IN ("
        "'S', 'SICK', 'SICK_LEAVE', 'MEDICAL', "
        "'M', 'MATERNITY', "
        "'P', 'Y', 'PATERNITY', 'PATERNITY_LEAVE'"
        ")"
    ))
    op.execute(sa.text(
        "UPDATE hr_leave_type SET balance_renewal_policy = 'MANUAL' "
        "WHERE UPPER(code) IN ('COMPENSATORY', 'COMPENSATION')"
    ))
    op.execute(sa.text(
        "UPDATE hr_leave_type SET balance_renewal_policy = 'ONCE' "
        "WHERE UPPER(code) IN ('H', 'HAJJ', 'HAJJ_LEAVE')"
    ))
    # SQLite cannot drop a column default without rebuilding the table.  The
    # harmless default is retained there; other dialects keep the model-level
    # default only after the migration completes.
    if bind.dialect.name != "sqlite":
        op.alter_column("hr_leave_type", "balance_renewal_policy", server_default=None)


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("hr_leave_type"):
        columns = {column["name"] for column in inspector.get_columns("hr_leave_type")}
        if "balance_renewal_policy" in columns:
            op.drop_column("hr_leave_type", "balance_renewal_policy")
