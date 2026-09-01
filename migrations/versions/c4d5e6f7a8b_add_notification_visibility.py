"""add visibility flag for independently delivered notifications

Revision ID: c4d5e6f7a8b
Revises: ab1c2d3e4f5a
Create Date: 2026-09-01 23:30:00
"""

from alembic import op
import sqlalchemy as sa


revision = "c4d5e6f7a8b"
down_revision = "ab1c2d3e4f5a"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "notification",
        sa.Column(
            "is_visible",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade():
    op.drop_column("notification", "is_visible")
