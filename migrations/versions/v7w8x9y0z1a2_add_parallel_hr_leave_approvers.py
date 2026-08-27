"""add parallel approvers to HR request steps

Revision ID: v7w8x9y0z1a2
Revises: u6v7w8x9y0z1
Create Date: 2026-08-27 23:10:00
"""

from alembic import op
import sqlalchemy as sa


revision = "v7w8x9y0z1a2"
down_revision = "u6v7w8x9y0z1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("hr_request_approval_step") as batch_op:
        batch_op.add_column(sa.Column("approver_user_ids", sa.Text(), nullable=True))

    connection = op.get_bind()
    connection.execute(sa.text(
        "UPDATE hr_request_approval_step "
        "SET approver_user_ids = CAST(approver_user_id AS VARCHAR) "
        "WHERE approver_user_id IS NOT NULL"
    ))


def downgrade():
    with op.batch_alter_table("hr_request_approval_step") as batch_op:
        batch_op.drop_column("approver_user_ids")
