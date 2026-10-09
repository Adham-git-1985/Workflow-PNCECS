"""preserve original HR approval candidates across escalation

Revision ID: zb2c3d4e5f6
Revises: za1b2c3d4e5f
Create Date: 2026-09-28
"""

from alembic import op
import sqlalchemy as sa


revision = "zb2c3d4e5f6"
down_revision = "za1b2c3d4e5f"
branch_labels = None
depends_on = None


TABLE = "hr_request_approval_step"
COLUMN = "initial_approver_user_ids"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in inspector.get_table_names():
        return

    columns = {
        column["name"] for column in inspector.get_columns(TABLE)
    }
    if COLUMN not in columns:
        with op.batch_alter_table(TABLE) as batch_op:
            batch_op.add_column(
                sa.Column(COLUMN, sa.Text(), nullable=True)
            )
        columns.add(COLUMN)

    if {
        COLUMN,
        "approver_user_ids",
        "escalation_count",
    }.issubset(columns):
        op.execute(sa.text(
            "UPDATE hr_request_approval_step "
            "SET initial_approver_user_ids = approver_user_ids "
            "WHERE initial_approver_user_ids IS NULL "
            "AND COALESCE(escalation_count, 0) = 0 "
            "AND approver_user_ids IS NOT NULL"
        ))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in inspector.get_table_names():
        return
    columns = {
        column["name"] for column in inspector.get_columns(TABLE)
    }
    if COLUMN in columns:
        with op.batch_alter_table(TABLE) as batch_op:
            batch_op.drop_column(COLUMN)
