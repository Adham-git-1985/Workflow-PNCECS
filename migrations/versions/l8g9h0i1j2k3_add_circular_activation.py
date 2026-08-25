"""Add activation state to portal circulars.

Revision ID: l8g9h0i1j2k3
Revises: k7f8g9h0i1j2
"""

from alembic import op
import sqlalchemy as sa


revision = "l8g9h0i1j2k3"
down_revision = "k7f8g9h0i1j2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("portal_circulars", schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ))
        batch_op.create_index(
            "ix_portal_circulars_is_active",
            ["is_active"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("portal_circulars", schema=None) as batch_op:
        batch_op.drop_index("ix_portal_circulars_is_active")
        batch_op.drop_column("is_active")
