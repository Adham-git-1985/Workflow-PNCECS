"""link official outbound reply to inbound correspondence

Revision ID: a1b2c3d4e5f6
Revises: 9c4d5e6f7081
Create Date: 2026-08-16 18:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = "a1b2c3d4e5f6"
down_revision = "9c4d5e6f7081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("corr_outbound", schema=None) as batch_op:
        batch_op.add_column(sa.Column("source_inbound_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_corr_outbound_source_inbound_id",
            "corr_inbound",
            ["source_inbound_id"],
            ["id"],
        )
        batch_op.create_index(
            "ix_corr_outbound_source_inbound_id",
            ["source_inbound_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("corr_outbound", schema=None) as batch_op:
        batch_op.drop_index("ix_corr_outbound_source_inbound_id")
        batch_op.drop_constraint(
            "fk_corr_outbound_source_inbound_id",
            type_="foreignkey",
        )
        batch_op.drop_column("source_inbound_id")
