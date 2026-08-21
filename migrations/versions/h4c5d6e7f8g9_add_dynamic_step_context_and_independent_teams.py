"""add dynamic step context and independent teams

Revision ID: h4c5d6e7f8g9
Revises: g3b4c5d6e7f8
Create Date: 2026-08-21
"""

from alembic import op
import sqlalchemy as sa


revision = "h4c5d6e7f8g9"
down_revision = "g3b4c5d6e7f8"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    step_columns = {column["name"] for column in inspector.get_columns("workflow_instance_steps")}
    with op.batch_alter_table("workflow_instance_steps") as batch_op:
        if "routing_label" not in step_columns:
            batch_op.add_column(sa.Column("routing_label", sa.String(length=200), nullable=True))
        if "routing_job_title" not in step_columns:
            batch_op.add_column(sa.Column("routing_job_title", sa.String(length=200), nullable=True))
        if "routing_node_label" not in step_columns:
            batch_op.add_column(sa.Column("routing_node_label", sa.Text(), nullable=True))
        if "routing_reason" not in step_columns:
            batch_op.add_column(sa.Column("routing_reason", sa.Text(), nullable=True))

    team_columns = {column["name"]: column for column in inspector.get_columns("teams")}
    if team_columns.get("section_id", {}).get("nullable") is False:
        with op.batch_alter_table("teams") as batch_op:
            batch_op.alter_column("section_id", existing_type=sa.Integer(), nullable=True)


def downgrade():
    with op.batch_alter_table("workflow_instance_steps") as batch_op:
        batch_op.drop_column("routing_reason")
        batch_op.drop_column("routing_node_label")
        batch_op.drop_column("routing_job_title")
        batch_op.drop_column("routing_label")
