"""add correspondence attachment stamp fields

Revision ID: 4d9e0f1a2b34
Revises: 3c8d9e0f1a23
Create Date: 2026-06-24 00:20:00

"""
from alembic import op
import sqlalchemy as sa


revision = "4d9e0f1a2b34"
down_revision = "3c8d9e0f1a23"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("corr_attachment", schema=None) as batch_op:
        batch_op.add_column(sa.Column("stamp_applied", sa.Boolean(), nullable=False, server_default=sa.text("0")))
        batch_op.add_column(sa.Column("stamp_kind", sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column("stamp_ref_no", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("stamp_date", sa.String(length=10), nullable=True))

    with op.batch_alter_table("corr_attachment", schema=None) as batch_op:
        batch_op.alter_column("stamp_applied", server_default=None)


def downgrade():
    with op.batch_alter_table("corr_attachment", schema=None) as batch_op:
        batch_op.drop_column("stamp_date")
        batch_op.drop_column("stamp_ref_no")
        batch_op.drop_column("stamp_kind")
        batch_op.drop_column("stamp_applied")
