"""link correspondence to workflow templates and secure confidential access

Revision ID: 7a2b3c4d5e6f
Revises: 6f1a2b3c4d5e
Create Date: 2026-08-05 00:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = "7a2b3c4d5e6f"
down_revision = "6f1a2b3c4d5e"
branch_labels = None
depends_on = None


def _add_template_column(table_name: str) -> None:
    with op.batch_alter_table(table_name, schema=None) as batch_op:
        batch_op.add_column(sa.Column("workflow_template_id", sa.Integer(), nullable=True))
        batch_op.create_index(
            f"ix_{table_name}_workflow_template_id",
            ["workflow_template_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            f"fk_{table_name}_workflow_template",
            "workflow_templates",
            ["workflow_template_id"],
            ["id"],
        )


def upgrade() -> None:
    _add_template_column("corr_inbound")
    _add_template_column("corr_outbound")

    op.create_table(
        "corr_confidential_access",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("inbound_id", sa.Integer(), nullable=True),
        sa.Column("outbound_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("granted_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "(inbound_id IS NOT NULL AND outbound_id IS NULL) OR "
            "(inbound_id IS NULL AND outbound_id IS NOT NULL)",
            name="ck_corr_confidential_access_parent",
        ),
        sa.ForeignKeyConstraint(["granted_by_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["inbound_id"], ["corr_inbound.id"]),
        sa.ForeignKeyConstraint(["outbound_id"], ["corr_outbound.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("inbound_id", "user_id", name="uq_corr_confidential_inbound_user"),
        sa.UniqueConstraint("outbound_id", "user_id", name="uq_corr_confidential_outbound_user"),
    )
    for column in ("inbound_id", "outbound_id", "user_id", "granted_by_id", "created_at"):
        op.create_index(
            f"ix_corr_confidential_access_{column}",
            "corr_confidential_access",
            [column],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("corr_confidential_access")
    for table_name in ("corr_outbound", "corr_inbound"):
        with op.batch_alter_table(table_name, schema=None) as batch_op:
            batch_op.drop_constraint(f"fk_{table_name}_workflow_template", type_="foreignkey")
            batch_op.drop_index(f"ix_{table_name}_workflow_template_id")
            batch_op.drop_column("workflow_template_id")
