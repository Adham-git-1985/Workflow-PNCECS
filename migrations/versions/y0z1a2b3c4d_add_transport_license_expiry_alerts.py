"""add transport licence expiry alerts

Revision ID: y0z1a2b3c4d
Revises: x9y0z1a2b3c
Create Date: 2026-08-30 14:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "y0z1a2b3c4d"
down_revision = "x9y0z1a2b3c"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("transport_driver") as batch_op:
        batch_op.add_column(sa.Column("license_end_day", sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column("license_alert_sent_for", sa.String(length=10), nullable=True))
        batch_op.create_index("ix_transport_driver_license_end_day", ["license_end_day"], unique=False)

    with op.batch_alter_table("transport_vehicle") as batch_op:
        batch_op.add_column(sa.Column("license_alert_sent_for", sa.String(length=10), nullable=True))


def downgrade():
    with op.batch_alter_table("transport_vehicle") as batch_op:
        batch_op.drop_column("license_alert_sent_for")

    with op.batch_alter_table("transport_driver") as batch_op:
        batch_op.drop_index("ix_transport_driver_license_end_day")
        batch_op.drop_column("license_alert_sent_for")
        batch_op.drop_column("license_end_day")
