"""add links to portal notifications

Revision ID: g3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-08-19
"""

from alembic import op
import sqlalchemy as sa


revision = "g3b4c5d6e7f8"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("notification")}
    if "link_url" not in columns:
        with op.batch_alter_table("notification") as batch_op:
            batch_op.add_column(sa.Column("link_url", sa.String(length=500), nullable=True))


def downgrade():
    with op.batch_alter_table("notification") as batch_op:
        batch_op.drop_column("link_url")
