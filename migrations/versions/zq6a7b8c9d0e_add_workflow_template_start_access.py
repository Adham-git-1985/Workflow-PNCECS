"""add start-access rules for predefined workflow templates

Revision ID: zq6a7b8c9d0e
Revises: zp5a6b7c8d9e
Create Date: 2026-10-27 12:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "zq6a7b8c9d0e"
down_revision = "zp5a6b7c8d9e"
branch_labels = None
depends_on = None


def _table_names(bind):
    return set(sa.inspect(bind).get_table_names())


def _column_names(bind, table_name):
    return {
        column["name"]
        for column in sa.inspect(bind).get_columns(table_name)
    }


def upgrade():
    bind = op.get_bind()
    tables = _table_names(bind)

    if "workflow_templates" in tables and "start_access_mode" not in _column_names(
        bind, "workflow_templates"
    ):
        # Existing templates are locked until an administrator explicitly
        # chooses their start audience.
        op.add_column(
            "workflow_templates",
            sa.Column(
                "start_access_mode",
                sa.String(length=20),
                nullable=False,
                server_default=sa.text("'NONE'"),
            ),
        )

    if "workflow_template_start_access" not in tables:
        op.create_table(
            "workflow_template_start_access",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("template_id", sa.Integer(), nullable=False),
            sa.Column("audience_type", sa.String(length=20), nullable=False),
            sa.Column("audience_id", sa.Integer(), nullable=False),
            sa.Column("created_by_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["created_by_id"],
                ["users.id"],
            ),
            sa.ForeignKeyConstraint(
                ["template_id"],
                ["workflow_templates.id"],
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "template_id",
                "audience_type",
                "audience_id",
                name="uq_workflow_template_start_access_audience",
            ),
            sa.CheckConstraint(
                "audience_type IN ('ORGANIZATION','DIRECTORATE','DEPARTMENT','SECTION','USER')",
                name="ck_workflow_template_start_access_type",
            ),
        )
        op.create_index(
            "ix_workflow_template_start_access_template_id",
            "workflow_template_start_access",
            ["template_id"],
            unique=False,
        )
        op.create_index(
            "ix_workflow_template_start_access_template_type",
            "workflow_template_start_access",
            ["template_id", "audience_type"],
            unique=False,
        )


def downgrade():
    bind = op.get_bind()
    if "workflow_template_start_access" in _table_names(bind):
        op.drop_index(
            "ix_workflow_template_start_access_template_type",
            table_name="workflow_template_start_access",
        )
        op.drop_index(
            "ix_workflow_template_start_access_template_id",
            table_name="workflow_template_start_access",
        )
        op.drop_table("workflow_template_start_access")

    if "workflow_templates" in _table_names(bind) and "start_access_mode" in _column_names(
        bind, "workflow_templates"
    ):
        op.drop_column("workflow_templates", "start_access_mode")
