"""add approved mobile devices for fixed asset stocktaking

Revision ID: zk0f1a2b3c4d
Revises: zj9e0f1a2b3
Create Date: 2026-10-15
"""

from alembic import op
import sqlalchemy as sa


revision = "zk0f1a2b3c4d"
down_revision = "zj9e0f1a2b3"
branch_labels = None
depends_on = None


def _table_names():
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade():
    if "inv_fixed_asset_mobile_device" in _table_names():
        return

    op.create_table(
        "inv_fixed_asset_mobile_device",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("device_label", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="PENDING", nullable=False),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("approved_by_id", sa.Integer(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("last_ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["approved_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_inv_fixed_asset_mobile_device_token_hash",
        "inv_fixed_asset_mobile_device",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_inv_fixed_asset_mobile_device_user_id",
        "inv_fixed_asset_mobile_device",
        ["user_id"],
    )
    op.create_index(
        "ix_inv_fixed_asset_mobile_device_status",
        "inv_fixed_asset_mobile_device",
        ["status"],
    )
    op.create_index(
        "ix_inv_fixed_asset_mobile_device_requested_at",
        "inv_fixed_asset_mobile_device",
        ["requested_at"],
    )
    op.create_index(
        "ix_inv_fixed_asset_mobile_device_approved_by_id",
        "inv_fixed_asset_mobile_device",
        ["approved_by_id"],
    )
    op.create_index(
        "ix_inv_fixed_asset_mobile_device_status_requested",
        "inv_fixed_asset_mobile_device",
        ["status", "requested_at"],
    )


def downgrade():
    if "inv_fixed_asset_mobile_device" in _table_names():
        op.drop_table("inv_fixed_asset_mobile_device")
