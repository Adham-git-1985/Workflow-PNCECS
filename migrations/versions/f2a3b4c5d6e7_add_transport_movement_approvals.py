"""add staged transport movement approvals

Revision ID: f2a3b4c5d6e7
Revises: b7c8d9e0f1a2, d4e5f6a7b8c9, f1a2b3c4d5e6
Create Date: 2026-08-19
"""

from alembic import op
import sqlalchemy as sa


revision = "f2a3b4c5d6e7"
down_revision = ("b7c8d9e0f1a2", "d4e5f6a7b8c9", "f1a2b3c4d5e6")
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("transport_permit")}
    with op.batch_alter_table("transport_permit") as batch_op:
        if "approval_stage" not in columns:
            batch_op.add_column(sa.Column("approval_stage", sa.String(length=30), nullable=False, server_default="MANAGER"))
        if "manager_user_id" not in columns:
            batch_op.add_column(sa.Column("manager_user_id", sa.Integer(), nullable=True))

    if not inspector.has_table("transport_permit_action"):
        op.create_table(
            "transport_permit_action",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("permit_id", sa.Integer(), nullable=False),
            sa.Column("stage", sa.String(length=30), nullable=False),
            sa.Column("action", sa.String(length=20), nullable=False),
            sa.Column("actor_user_id", sa.Integer(), nullable=False),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["permit_id"], ["transport_permit.id"]),
            sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        )


def downgrade():
    op.drop_table("transport_permit_action")
    with op.batch_alter_table("transport_permit") as batch_op:
        batch_op.drop_column("manager_user_id")
        batch_op.drop_column("approval_stage")
