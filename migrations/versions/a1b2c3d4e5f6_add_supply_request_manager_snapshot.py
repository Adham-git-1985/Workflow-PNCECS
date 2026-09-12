"""store parallel direct-manager recipients for supply requests

Revision ID: a1b2c3d4e5f6
Revises: z1a2b3c4d5e6
Create Date: 2026-09-12 12:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "a1b2c3d4e5f6"
down_revision = "z1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("inv_employee_request")}
    if "manager_user_ids" not in columns:
        op.add_column("inv_employee_request", sa.Column("manager_user_ids", sa.Text(), nullable=True))


def downgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("inv_employee_request")}
    if "manager_user_ids" in columns:
        op.drop_column("inv_employee_request", "manager_user_ids")
