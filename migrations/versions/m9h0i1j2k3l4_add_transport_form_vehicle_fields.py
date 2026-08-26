"""add transport form vehicle fields

Revision ID: m9h0i1j2k3l4
Revises: l8g9h0i1j2k3
Create Date: 2026-08-26 12:45:00
"""

from alembic import op
import sqlalchemy as sa


revision = "m9h0i1j2k3l4"
down_revision = "l8g9h0i1j2k3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("transport_vehicle") as batch_op:
        batch_op.add_column(sa.Column("chassis_no", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("engine_no", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("odometer_no", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("assigned_to", sa.String(length=200), nullable=True))
        batch_op.create_index("ix_transport_vehicle_chassis_no", ["chassis_no"], unique=False)
        batch_op.create_index("ix_transport_vehicle_engine_no", ["engine_no"], unique=False)


def downgrade():
    with op.batch_alter_table("transport_vehicle") as batch_op:
        batch_op.drop_index("ix_transport_vehicle_engine_no")
        batch_op.drop_index("ix_transport_vehicle_chassis_no")
        batch_op.drop_column("assigned_to")
        batch_op.drop_column("odometer_no")
        batch_op.drop_column("engine_no")
        batch_op.drop_column("chassis_no")
