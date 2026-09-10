"""add fixed asset QR inventory

Revision ID: j1k2l3m4n5o6
Revises: i0j1k2l3m4n5
Create Date: 2026-09-10
"""

from alembic import op
import sqlalchemy as sa


revision = "j1k2l3m4n5o6"
down_revision = "i0j1k2l3m4n5"
branch_labels = None
depends_on = None


def _table_names():
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade():
    tables = _table_names()

    if "inv_fixed_asset" not in tables:
        op.create_table(
            "inv_fixed_asset",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("asset_tag", sa.String(length=80), nullable=False),
            sa.Column("qr_token", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("item_id", sa.Integer(), nullable=True),
            sa.Column("category_id", sa.Integer(), nullable=True),
            sa.Column("serial_number", sa.String(length=200), nullable=True),
            sa.Column("manufacturer", sa.String(length=150), nullable=True),
            sa.Column("model", sa.String(length=150), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("acquisition_date", sa.String(length=10), nullable=True),
            sa.Column("purchase_cost", sa.Numeric(precision=14, scale=2), nullable=True),
            sa.Column("asset_condition", sa.String(length=30), server_default="GOOD", nullable=False),
            sa.Column("lifecycle_status", sa.String(length=30), server_default="ACTIVE", nullable=False),
            sa.Column("warehouse_id", sa.Integer(), nullable=True),
            sa.Column("room_id", sa.Integer(), nullable=True),
            sa.Column("custodian_user_id", sa.Integer(), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
            sa.Column("created_by_id", sa.Integer(), nullable=True),
            sa.Column("updated_by_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["item_id"], ["inv_item.id"]),
            sa.ForeignKeyConstraint(["category_id"], ["inv_item_category.id"]),
            sa.ForeignKeyConstraint(["warehouse_id"], ["inv_warehouse.id"]),
            sa.ForeignKeyConstraint(["room_id"], ["inv_room.id"]),
            sa.ForeignKeyConstraint(["custodian_user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["created_by_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_inv_fixed_asset_asset_tag", "inv_fixed_asset", ["asset_tag"], unique=True)
        op.create_index("ix_inv_fixed_asset_qr_token", "inv_fixed_asset", ["qr_token"], unique=True)
        op.create_index("ix_inv_fixed_asset_name", "inv_fixed_asset", ["name"])
        op.create_index("ix_inv_fixed_asset_item_id", "inv_fixed_asset", ["item_id"])
        op.create_index("ix_inv_fixed_asset_category_id", "inv_fixed_asset", ["category_id"])
        op.create_index("ix_inv_fixed_asset_serial_number", "inv_fixed_asset", ["serial_number"])
        op.create_index("ix_inv_fixed_asset_acquisition_date", "inv_fixed_asset", ["acquisition_date"])
        op.create_index("ix_inv_fixed_asset_asset_condition", "inv_fixed_asset", ["asset_condition"])
        op.create_index("ix_inv_fixed_asset_lifecycle_status", "inv_fixed_asset", ["lifecycle_status"])
        op.create_index("ix_inv_fixed_asset_warehouse_id", "inv_fixed_asset", ["warehouse_id"])
        op.create_index("ix_inv_fixed_asset_room_id", "inv_fixed_asset", ["room_id"])
        op.create_index("ix_inv_fixed_asset_custodian_user_id", "inv_fixed_asset", ["custodian_user_id"])
        op.create_index("ix_inv_fixed_asset_is_active", "inv_fixed_asset", ["is_active"])
        op.create_index("ix_inv_fixed_asset_created_by_id", "inv_fixed_asset", ["created_by_id"])
        op.create_index("ix_inv_fixed_asset_updated_by_id", "inv_fixed_asset", ["updated_by_id"])
        op.create_index("ix_inv_fixed_asset_created_at", "inv_fixed_asset", ["created_at"])
        op.create_index("ix_inv_fixed_asset_updated_at", "inv_fixed_asset", ["updated_at"])
        op.create_index("ix_inv_fixed_asset_scope", "inv_fixed_asset", ["is_active", "lifecycle_status", "warehouse_id", "room_id"])
        op.create_index("ix_inv_fixed_asset_name_serial", "inv_fixed_asset", ["name", "serial_number"])

    if "inv_fixed_asset_cycle" not in tables:
        op.create_table(
            "inv_fixed_asset_cycle",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("code", sa.String(length=80), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("inventory_date", sa.String(length=10), nullable=False),
            sa.Column("committee_name", sa.String(length=255), nullable=True),
            sa.Column("previous_cycle_id", sa.Integer(), nullable=True),
            sa.Column("scope_warehouse_id", sa.Integer(), nullable=True),
            sa.Column("scope_room_id", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(length=20), server_default="ACTIVE", nullable=False),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("closed_at", sa.DateTime(), nullable=True),
            sa.Column("created_by_id", sa.Integer(), nullable=True),
            sa.Column("closed_by_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["previous_cycle_id"], ["inv_fixed_asset_cycle.id"]),
            sa.ForeignKeyConstraint(["scope_warehouse_id"], ["inv_warehouse.id"]),
            sa.ForeignKeyConstraint(["scope_room_id"], ["inv_room.id"]),
            sa.ForeignKeyConstraint(["created_by_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["closed_by_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_inv_fixed_asset_cycle_code", "inv_fixed_asset_cycle", ["code"], unique=True)
        op.create_index("ix_inv_fixed_asset_cycle_name", "inv_fixed_asset_cycle", ["name"])
        op.create_index("ix_inv_fixed_asset_cycle_inventory_date", "inv_fixed_asset_cycle", ["inventory_date"])
        op.create_index("ix_inv_fixed_asset_cycle_previous_cycle_id", "inv_fixed_asset_cycle", ["previous_cycle_id"])
        op.create_index("ix_inv_fixed_asset_cycle_scope_warehouse_id", "inv_fixed_asset_cycle", ["scope_warehouse_id"])
        op.create_index("ix_inv_fixed_asset_cycle_scope_room_id", "inv_fixed_asset_cycle", ["scope_room_id"])
        op.create_index("ix_inv_fixed_asset_cycle_status", "inv_fixed_asset_cycle", ["status"])
        op.create_index("ix_inv_fixed_asset_cycle_started_at", "inv_fixed_asset_cycle", ["started_at"])
        op.create_index("ix_inv_fixed_asset_cycle_closed_at", "inv_fixed_asset_cycle", ["closed_at"])
        op.create_index("ix_inv_fixed_asset_cycle_created_by_id", "inv_fixed_asset_cycle", ["created_by_id"])
        op.create_index("ix_inv_fixed_asset_cycle_closed_by_id", "inv_fixed_asset_cycle", ["closed_by_id"])
        op.create_index("ix_inv_fixed_asset_cycle_created_at", "inv_fixed_asset_cycle", ["created_at"])
        op.create_index("ix_inv_fixed_asset_cycle_date_status", "inv_fixed_asset_cycle", ["inventory_date", "status"])

    if "inv_fixed_asset_cycle_member" not in tables:
        op.create_table(
            "inv_fixed_asset_cycle_member",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("cycle_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("member_role", sa.String(length=20), server_default="MEMBER", nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["cycle_id"], ["inv_fixed_asset_cycle.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("cycle_id", "user_id", name="uq_inv_fixed_asset_cycle_member"),
        )
        op.create_index("ix_inv_fixed_asset_cycle_member_cycle_id", "inv_fixed_asset_cycle_member", ["cycle_id"])
        op.create_index("ix_inv_fixed_asset_cycle_member_user_id", "inv_fixed_asset_cycle_member", ["user_id"])
        op.create_index("ix_inv_fixed_asset_cycle_member_member_role", "inv_fixed_asset_cycle_member", ["member_role"])

    if "inv_fixed_asset_entry" not in tables:
        op.create_table(
            "inv_fixed_asset_entry",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("cycle_id", sa.Integer(), nullable=False),
            sa.Column("asset_id", sa.Integer(), nullable=False),
            sa.Column("was_expected", sa.Boolean(), server_default=sa.true(), nullable=False),
            sa.Column("previous_result_status", sa.String(length=40), nullable=True),
            sa.Column("expected_warehouse_id", sa.Integer(), nullable=True),
            sa.Column("expected_room_id", sa.Integer(), nullable=True),
            sa.Column("expected_custodian_user_id", sa.Integer(), nullable=True),
            sa.Column("expected_condition", sa.String(length=30), nullable=True),
            sa.Column("expected_lifecycle_status", sa.String(length=30), nullable=True),
            sa.Column("observed_warehouse_id", sa.Integer(), nullable=True),
            sa.Column("observed_room_id", sa.Integer(), nullable=True),
            sa.Column("observed_custodian_user_id", sa.Integer(), nullable=True),
            sa.Column("observed_condition", sa.String(length=30), nullable=True),
            sa.Column("observed_lifecycle_status", sa.String(length=30), nullable=True),
            sa.Column("result_status", sa.String(length=40), server_default="PENDING", nullable=False),
            sa.Column("scan_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("last_scan_source", sa.String(length=20), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("scanned_at", sa.DateTime(), nullable=True),
            sa.Column("scanned_by_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["cycle_id"], ["inv_fixed_asset_cycle.id"]),
            sa.ForeignKeyConstraint(["asset_id"], ["inv_fixed_asset.id"]),
            sa.ForeignKeyConstraint(["expected_warehouse_id"], ["inv_warehouse.id"]),
            sa.ForeignKeyConstraint(["expected_room_id"], ["inv_room.id"]),
            sa.ForeignKeyConstraint(["expected_custodian_user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["observed_warehouse_id"], ["inv_warehouse.id"]),
            sa.ForeignKeyConstraint(["observed_room_id"], ["inv_room.id"]),
            sa.ForeignKeyConstraint(["observed_custodian_user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["scanned_by_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("cycle_id", "asset_id", name="uq_inv_fixed_asset_entry_cycle_asset"),
        )
        op.create_index("ix_inv_fixed_asset_entry_cycle_result", "inv_fixed_asset_entry", ["cycle_id", "result_status"])
        op.create_index("ix_inv_fixed_asset_entry_cycle_id", "inv_fixed_asset_entry", ["cycle_id"])
        op.create_index("ix_inv_fixed_asset_entry_asset_id", "inv_fixed_asset_entry", ["asset_id"])
        op.create_index("ix_inv_fixed_asset_entry_was_expected", "inv_fixed_asset_entry", ["was_expected"])
        op.create_index("ix_inv_fixed_asset_entry_previous_result_status", "inv_fixed_asset_entry", ["previous_result_status"])
        op.create_index("ix_inv_fixed_asset_entry_result_status", "inv_fixed_asset_entry", ["result_status"])
        op.create_index("ix_inv_fixed_asset_entry_scanned_at", "inv_fixed_asset_entry", ["scanned_at"])
        op.create_index("ix_inv_fixed_asset_entry_scanned_by_id", "inv_fixed_asset_entry", ["scanned_by_id"])

    if "inv_fixed_asset_scan_log" not in tables:
        op.create_table(
            "inv_fixed_asset_scan_log",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("entry_id", sa.Integer(), nullable=False),
            sa.Column("cycle_id", sa.Integer(), nullable=False),
            sa.Column("asset_id", sa.Integer(), nullable=False),
            sa.Column("scanned_by_id", sa.Integer(), nullable=True),
            sa.Column("scanned_at", sa.DateTime(), nullable=False),
            sa.Column("source", sa.String(length=20), server_default="MANUAL", nullable=False),
            sa.Column("previous_result_status", sa.String(length=40), nullable=True),
            sa.Column("result_status", sa.String(length=40), nullable=False),
            sa.Column("observed_warehouse_id", sa.Integer(), nullable=True),
            sa.Column("observed_room_id", sa.Integer(), nullable=True),
            sa.Column("observed_custodian_user_id", sa.Integer(), nullable=True),
            sa.Column("observed_condition", sa.String(length=30), nullable=True),
            sa.Column("observed_lifecycle_status", sa.String(length=30), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("device_info", sa.String(length=255), nullable=True),
            sa.ForeignKeyConstraint(["entry_id"], ["inv_fixed_asset_entry.id"]),
            sa.ForeignKeyConstraint(["cycle_id"], ["inv_fixed_asset_cycle.id"]),
            sa.ForeignKeyConstraint(["asset_id"], ["inv_fixed_asset.id"]),
            sa.ForeignKeyConstraint(["scanned_by_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["observed_warehouse_id"], ["inv_warehouse.id"]),
            sa.ForeignKeyConstraint(["observed_room_id"], ["inv_room.id"]),
            sa.ForeignKeyConstraint(["observed_custodian_user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_inv_fixed_asset_scan_cycle_time", "inv_fixed_asset_scan_log", ["cycle_id", "scanned_at"])
        op.create_index("ix_inv_fixed_asset_scan_log_entry_id", "inv_fixed_asset_scan_log", ["entry_id"])
        op.create_index("ix_inv_fixed_asset_scan_log_cycle_id", "inv_fixed_asset_scan_log", ["cycle_id"])
        op.create_index("ix_inv_fixed_asset_scan_log_asset_id", "inv_fixed_asset_scan_log", ["asset_id"])
        op.create_index("ix_inv_fixed_asset_scan_log_scanned_by_id", "inv_fixed_asset_scan_log", ["scanned_by_id"])
        op.create_index("ix_inv_fixed_asset_scan_log_scanned_at", "inv_fixed_asset_scan_log", ["scanned_at"])


def downgrade():
    tables = _table_names()
    for table_name in (
        "inv_fixed_asset_scan_log",
        "inv_fixed_asset_entry",
        "inv_fixed_asset_cycle_member",
        "inv_fixed_asset_cycle",
        "inv_fixed_asset",
    ):
        if table_name in tables:
            op.drop_table(table_name)
