"""add meeting minutes attachments

Revision ID: 9c4d5e6f7081
Revises: 8b3c4d5e6f70
Create Date: 2026-08-12 00:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = "9c4d5e6f7081"
down_revision = "8b3c4d5e6f70"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "portal_meeting_attachments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("meeting_id", sa.Integer(), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("stored_name", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("uploaded_by_user_id", sa.Integer(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["meeting_id"], ["portal_meetings.id"]),
        sa.ForeignKeyConstraint(["uploaded_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stored_name"),
    )
    op.create_index(
        "ix_portal_meeting_attachments_meeting_id",
        "portal_meeting_attachments",
        ["meeting_id"],
        unique=False,
    )
    op.create_index(
        "ix_portal_meeting_attachments_uploaded_by_user_id",
        "portal_meeting_attachments",
        ["uploaded_by_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_portal_meeting_attachments_meeting",
        "portal_meeting_attachments",
        ["meeting_id", "uploaded_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_portal_meeting_attachments_meeting",
        table_name="portal_meeting_attachments",
    )
    op.drop_index(
        "ix_portal_meeting_attachments_uploaded_by_user_id",
        table_name="portal_meeting_attachments",
    )
    op.drop_index(
        "ix_portal_meeting_attachments_meeting_id",
        table_name="portal_meeting_attachments",
    )
    op.drop_table("portal_meeting_attachments")
