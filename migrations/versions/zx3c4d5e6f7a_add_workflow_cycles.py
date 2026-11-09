"""group correspondence workflows into coloured cycles

Revision ID: zx3c4d5e6f7a
Revises: zw2b3c4d5e6f
Create Date: 2026-11-09 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "zx3c4d5e6f7a"
down_revision = "zw2b3c4d5e6f"
branch_labels = None
depends_on = None


def _columns(bind):
    return {column["name"] for column in sa.inspect(bind).get_columns("workflow_request")}


def upgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "workflow_request" not in tables:
        return

    columns = _columns(bind)
    if "parent_request_id" not in columns:
        op.add_column(
            "workflow_request",
            sa.Column("parent_request_id", sa.Integer(), nullable=True),
        )
    if "cycle_number" not in columns:
        op.add_column(
            "workflow_request",
            sa.Column(
                "cycle_number",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            ),
        )
    if "cycle_color" not in columns:
        op.add_column(
            "workflow_request",
            sa.Column("cycle_color", sa.String(length=30), nullable=True),
        )
    if "cycle_opened_at" not in columns:
        op.add_column(
            "workflow_request",
            sa.Column("cycle_opened_at", sa.DateTime(), nullable=True),
        )

    inspector = sa.inspect(bind)
    index_names = {index["name"] for index in inspector.get_indexes("workflow_request")}
    if "ix_workflow_request_parent_request_id" not in index_names:
        op.create_index(
            "ix_workflow_request_parent_request_id",
            "workflow_request",
            ["parent_request_id"],
        )
    if "ix_workflow_request_cycle_group" not in index_names:
        op.create_index(
            "ix_workflow_request_cycle_group",
            "workflow_request",
            ["parent_request_id", "cycle_number"],
        )


def downgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "workflow_request" not in tables:
        return
    index_names = {index["name"] for index in sa.inspect(bind).get_indexes("workflow_request")}
    if "ix_workflow_request_cycle_group" in index_names:
        op.drop_index("ix_workflow_request_cycle_group", table_name="workflow_request")
    if "ix_workflow_request_parent_request_id" in index_names:
        op.drop_index(
            "ix_workflow_request_parent_request_id",
            table_name="workflow_request",
        )

    columns = _columns(bind)
    for name in ("cycle_opened_at", "cycle_color", "cycle_number", "parent_request_id"):
        if name in columns:
            op.drop_column("workflow_request", name)

