"""link employee-request returns to their original issue

Revision ID: m5n6o7p8q9r0
Revises: l4m5n6o7p8q9
Create Date: 2026-09-15
"""

from alembic import op
import sqlalchemy as sa


revision = "m5n6o7p8q9r0"
down_revision = "l4m5n6o7p8q9"
branch_labels = None
depends_on = None


def _columns(inspector, table_name):
    if table_name not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    voucher_indexes = {
        index["name"] for index in inspector.get_indexes("inv_return_voucher")
    } if "inv_return_voucher" in inspector.get_table_names() else set()
    line_indexes = {
        index["name"] for index in inspector.get_indexes("inv_return_voucher_line")
    } if "inv_return_voucher_line" in inspector.get_table_names() else set()

    voucher_columns = _columns(inspector, "inv_return_voucher")
    if "source_request_id" not in voucher_columns:
        op.add_column(
            "inv_return_voucher",
            sa.Column("source_request_id", sa.Integer(), nullable=True),
        )
    if "source_issue_voucher_id" not in voucher_columns:
        op.add_column(
            "inv_return_voucher",
            sa.Column("source_issue_voucher_id", sa.Integer(), nullable=True),
        )

    line_columns = _columns(inspector, "inv_return_voucher_line")
    if "source_request_line_id" not in line_columns:
        op.add_column(
            "inv_return_voucher_line",
            sa.Column("source_request_line_id", sa.Integer(), nullable=True),
        )

    if "inv_return_voucher" in inspector.get_table_names():
        if "ix_inv_return_voucher_source_request_id" not in voucher_indexes:
            op.create_index(
                "ix_inv_return_voucher_source_request_id",
                "inv_return_voucher",
                ["source_request_id"],
                unique=False,
            )
        if "ix_inv_return_voucher_source_issue_voucher_id" not in voucher_indexes:
            op.create_index(
                "ix_inv_return_voucher_source_issue_voucher_id",
                "inv_return_voucher",
                ["source_issue_voucher_id"],
                unique=False,
            )
    if "inv_return_voucher_line" in inspector.get_table_names():
        if "ix_inv_return_voucher_line_source_request_line_id" not in line_indexes:
            op.create_index(
                "ix_inv_return_voucher_line_source_request_line_id",
                "inv_return_voucher_line",
                ["source_request_line_id"],
                unique=False,
            )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "inv_return_voucher_line" in inspector.get_table_names():
        if "ix_inv_return_voucher_line_source_request_line_id" in {
            index["name"] for index in inspector.get_indexes("inv_return_voucher_line")
        }:
            op.drop_index(
                "ix_inv_return_voucher_line_source_request_line_id",
                table_name="inv_return_voucher_line",
            )
        if "source_request_line_id" in _columns(inspector, "inv_return_voucher_line"):
            with op.batch_alter_table("inv_return_voucher_line") as batch_op:
                batch_op.drop_column("source_request_line_id")
    if "inv_return_voucher" in inspector.get_table_names():
        indexes = {index["name"] for index in inspector.get_indexes("inv_return_voucher")}
        if "ix_inv_return_voucher_source_issue_voucher_id" in indexes:
            op.drop_index(
                "ix_inv_return_voucher_source_issue_voucher_id",
                table_name="inv_return_voucher",
            )
        if "ix_inv_return_voucher_source_request_id" in indexes:
            op.drop_index(
                "ix_inv_return_voucher_source_request_id",
                table_name="inv_return_voucher",
            )
        columns = _columns(inspector, "inv_return_voucher")
        if "source_issue_voucher_id" in columns or "source_request_id" in columns:
            with op.batch_alter_table("inv_return_voucher") as batch_op:
                if "source_issue_voucher_id" in columns:
                    batch_op.drop_column("source_issue_voucher_id")
                if "source_request_id" in columns:
                    batch_op.drop_column("source_request_id")
