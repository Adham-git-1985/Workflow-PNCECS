"""store employee follow-up operational section visibility

Revision ID: zv1a2b3c4d5e
Revises: zu0a1b2c3d4e
"""

from alembic import op
import sqlalchemy as sa


revision = "zv1a2b3c4d5e"
down_revision = "zu0a1b2c3d4e"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "employee_followup_reports" not in tables:
        return
    columns = {column["name"] for column in inspector.get_columns("employee_followup_reports")}
    if "operational_sections_json" not in columns:
        op.add_column(
            "employee_followup_reports",
            sa.Column("operational_sections_json", sa.Text(), nullable=True),
        )


def downgrade():
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "employee_followup_reports" not in tables:
        return
    columns = {column["name"] for column in inspector.get_columns("employee_followup_reports")}
    if "operational_sections_json" in columns:
        with op.batch_alter_table("employee_followup_reports", schema=None) as batch_op:
            batch_op.drop_column("operational_sections_json")
