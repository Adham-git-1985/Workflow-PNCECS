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


def upgrade():
    with op.batch_alter_table("hr_request_approval_step") as batch_op:
        batch_op.add_column(
            sa.Column("initial_approver_user_ids", sa.Text(), nullable=True)
        )

    op.execute(sa.text(
        "UPDATE hr_request_approval_step "
        "SET initial_approver_user_ids = approver_user_ids "
        "WHERE initial_approver_user_ids IS NULL "
        "AND COALESCE(escalation_count, 0) = 0 "
        "AND approver_user_ids IS NOT NULL"
    ))


def downgrade():
    with op.batch_alter_table("hr_request_approval_step") as batch_op:
        batch_op.drop_column("initial_approver_user_ids")
