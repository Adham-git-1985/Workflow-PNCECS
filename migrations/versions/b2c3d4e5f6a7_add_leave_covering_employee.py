"""store the employee covering an approved leave requester

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-09-12 12:15:00
"""

from alembic import op
import sqlalchemy as sa


revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("hr_leave_request")}
    if "covering_employee_name" not in columns:
        op.add_column("hr_leave_request", sa.Column("covering_employee_name", sa.String(length=200), nullable=True))


def downgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("hr_leave_request")}
    if "covering_employee_name" in columns:
        op.drop_column("hr_leave_request", "covering_employee_name")
