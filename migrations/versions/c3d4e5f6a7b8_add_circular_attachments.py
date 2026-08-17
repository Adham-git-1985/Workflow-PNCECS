"""add attachments to portal circulars

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-17 00:30:00

"""
from alembic import op
import sqlalchemy as sa


revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "portal_circular_attachments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("circular_id", sa.Integer(), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("stored_name", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("uploaded_by_user_id", sa.Integer(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["circular_id"], ["portal_circulars.id"]),
        sa.ForeignKeyConstraint(["uploaded_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stored_name"),
    )
    op.create_index(
        "ix_portal_circular_attachments_circular_id",
        "portal_circular_attachments",
        ["circular_id"],
        unique=False,
    )
    op.create_index(
        "ix_portal_circular_attachments_uploaded_by_user_id",
        "portal_circular_attachments",
        ["uploaded_by_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_portal_circular_attachments_circular_uploaded",
        "portal_circular_attachments",
        ["circular_id", "uploaded_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_portal_circular_attachments_circular_uploaded",
        table_name="portal_circular_attachments",
    )
    op.drop_index(
        "ix_portal_circular_attachments_uploaded_by_user_id",
        table_name="portal_circular_attachments",
    )
    op.drop_index(
        "ix_portal_circular_attachments_circular_id",
        table_name="portal_circular_attachments",
    )
    op.drop_table("portal_circular_attachments")
