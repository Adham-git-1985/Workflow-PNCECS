"""add signed inventory adjustment vouchers

Revision ID: zt9d0e1f2a3b
Revises: zs8c9d0e1f2, a4c5d6e7f8a9
"""

from alembic import op
import sqlalchemy as sa


revision = "zt9d0e1f2a3b"
down_revision = ("zs8c9d0e1f2", "a4c5d6e7f8a9")
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _create_index_if_missing(inspector, name, table, columns):
    indexes = {index["name"] for index in inspector.get_indexes(table)}
    if name not in indexes:
        op.create_index(name, table, columns, unique=False)


def upgrade():
    inspector = _inspector()
    tables = set(inspector.get_table_names())

    if "inv_adjustment_voucher" not in tables:
        op.create_table(
            "inv_adjustment_voucher",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("voucher_no", sa.String(length=80), nullable=False),
            sa.Column("voucher_date", sa.String(length=10), nullable=False),
            sa.Column("warehouse_id", sa.Integer(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("source_reference", sa.String(length=120), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("created_by_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["created_by_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["warehouse_id"], ["inv_warehouse.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        inspector = _inspector()

    _create_index_if_missing(
        inspector,
        "ix_inv_adjustment_voucher_voucher_no",
        "inv_adjustment_voucher",
        ["voucher_no"],
    )
    _create_index_if_missing(
        inspector,
        "ix_inv_adjustment_voucher_voucher_date",
        "inv_adjustment_voucher",
        ["voucher_date"],
    )
    _create_index_if_missing(
        inspector,
        "ix_inv_adjustment_voucher_warehouse_id",
        "inv_adjustment_voucher",
        ["warehouse_id"],
    )
    _create_index_if_missing(
        inspector,
        "ix_inv_adjustment_voucher_created_by_id",
        "inv_adjustment_voucher",
        ["created_by_id"],
    )
    _create_index_if_missing(
        inspector,
        "ix_inv_adjustment_voucher_created_at",
        "inv_adjustment_voucher",
        ["created_at"],
    )
    _create_index_if_missing(
        inspector,
        "ix_inv_adjustment_voucher_date_no",
        "inv_adjustment_voucher",
        ["voucher_date", "voucher_no"],
    )

    inspector = _inspector()
    tables = set(inspector.get_table_names())
    if "inv_adjustment_voucher_line" not in tables:
        op.create_table(
            "inv_adjustment_voucher_line",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("voucher_id", sa.Integer(), nullable=False),
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("delta_qty", sa.Float(), nullable=False),
            sa.Column("serial", sa.String(length=200), nullable=True),
            sa.Column("warranty_start", sa.String(length=10), nullable=True),
            sa.Column("warranty_end", sa.String(length=10), nullable=True),
            sa.Column("details", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(["item_id"], ["inv_item.id"]),
            sa.ForeignKeyConstraint(["voucher_id"], ["inv_adjustment_voucher.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        inspector = _inspector()

    _create_index_if_missing(
        inspector,
        "ix_inv_adjustment_voucher_line_voucher_id",
        "inv_adjustment_voucher_line",
        ["voucher_id"],
    )
    _create_index_if_missing(
        inspector,
        "ix_inv_adjustment_voucher_line_item_id",
        "inv_adjustment_voucher_line",
        ["item_id"],
    )

    inspector = _inspector()
    tables = set(inspector.get_table_names())
    if "inv_adjustment_voucher_attachment" not in tables:
        op.create_table(
            "inv_adjustment_voucher_attachment",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("voucher_id", sa.Integer(), nullable=False),
            sa.Column("original_name", sa.String(length=255), nullable=False),
            sa.Column("stored_name", sa.String(length=255), nullable=False),
            sa.Column("file_path", sa.String(length=500), nullable=False),
            sa.Column("uploaded_by_id", sa.Integer(), nullable=True),
            sa.Column("uploaded_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["uploaded_by_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["voucher_id"], ["inv_adjustment_voucher.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        inspector = _inspector()

    _create_index_if_missing(
        inspector,
        "ix_inv_adjustment_voucher_attachment_voucher_id",
        "inv_adjustment_voucher_attachment",
        ["voucher_id"],
    )
    _create_index_if_missing(
        inspector,
        "ix_inv_adjustment_voucher_attachment_uploaded_by_id",
        "inv_adjustment_voucher_attachment",
        ["uploaded_by_id"],
    )
    _create_index_if_missing(
        inspector,
        "ix_inv_adjustment_voucher_attachment_uploaded_at",
        "inv_adjustment_voucher_attachment",
        ["uploaded_at"],
    )


def downgrade():
    inspector = _inspector()
    tables = set(inspector.get_table_names())

    if "inv_adjustment_voucher_attachment" in tables:
        op.drop_table("inv_adjustment_voucher_attachment")
    if "inv_adjustment_voucher_line" in tables:
        op.drop_table("inv_adjustment_voucher_line")
    if "inv_adjustment_voucher" in tables:
        op.drop_table("inv_adjustment_voucher")
