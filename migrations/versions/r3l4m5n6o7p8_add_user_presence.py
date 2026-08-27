"""add user presence heartbeats

Revision ID: r3l4m5n6o7p8
Revises: q2k3l4m5n6o7
Create Date: 2026-08-27 11:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "r3l4m5n6o7p8"
down_revision = "q2k3l4m5n6o7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_presence",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_path", sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_index(
        "ix_user_presence_last_seen_at",
        "user_presence",
        ["last_seen_at"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_user_presence_last_seen_at", table_name="user_presence")
    op.drop_table("user_presence")
