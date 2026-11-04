"""remember meeting tasks removed from employee follow-up reports

Revision ID: zw2b3c4d5e6f
Revises: zv1a2b3c4d5e
"""

from alembic import op
import sqlalchemy as sa


revision = "zw2b3c4d5e6f"
down_revision = "zv1a2b3c4d5e"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "employee_followup_reports" not in tables:
        return
    columns = {
        column["name"]
        for column in inspector.get_columns("employee_followup_reports")
    }
    if "excluded_meeting_task_ids_json" not in columns:
        op.add_column(
            "employee_followup_reports",
            sa.Column("excluded_meeting_task_ids_json", sa.Text(), nullable=True),
        )


def downgrade():
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "employee_followup_reports" not in tables:
        return
    columns = {
        column["name"]
        for column in inspector.get_columns("employee_followup_reports")
    }
    if "excluded_meeting_task_ids_json" in columns:
        with op.batch_alter_table("employee_followup_reports", schema=None) as batch_op:
            batch_op.drop_column("excluded_meeting_task_ids_json")
