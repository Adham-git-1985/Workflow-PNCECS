"""add optional images to fixed assets

Revision ID: zl1f2a3b4c5d
Revises: zk0f1a2b3c4d
Create Date: 2026-10-15
"""

from alembic import op
import sqlalchemy as sa


revision = "zl1f2a3b4c5d"
down_revision = "zk0f1a2b3c4d"
branch_labels = None
depends_on = None


def _columns(table_name):
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "inv_fixed_asset" not in inspector.get_table_names():
        return
    if "image_filename" not in _columns("inv_fixed_asset"):
        op.add_column(
            "inv_fixed_asset",
            sa.Column("image_filename", sa.String(length=255), nullable=True),
        )


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if "inv_fixed_asset" in inspector.get_table_names() and "image_filename" in _columns("inv_fixed_asset"):
        op.drop_column("inv_fixed_asset", "image_filename")
