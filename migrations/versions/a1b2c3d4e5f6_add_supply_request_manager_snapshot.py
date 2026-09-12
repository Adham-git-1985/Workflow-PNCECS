"""store parallel direct-manager recipients for supply requests

Revision ID: e8f1c2d3a4b5
Revises: l2m3n4o5p6q7
Create Date: 2026-09-12 12:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "e8f1c2d3a4b5"
down_revision = "l2m3n4o5p6q7"
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
