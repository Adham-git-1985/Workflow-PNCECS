"""add internal message attachments

Revision ID: a2b3c4d5e6f
Revises: y0z1a2b3c4d
Create Date: 2026-08-31
"""

from alembic import op
import sqlalchemy as sa


revision = "a2b3c4d5e6f"
down_revision = "y0z1a2b3c4d"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "message_attachments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("stored_name", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("uploaded_by_id", sa.Integer(), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"]),
        sa.ForeignKeyConstraint(["uploaded_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stored_name"),
    )
    op.create_index("ix_message_attachments_message_id", "message_attachments", ["message_id"], unique=False)
    op.create_index("ix_message_attachments_uploaded_by_id", "message_attachments", ["uploaded_by_id"], unique=False)
    op.create_index("ix_message_attachments_uploaded_at", "message_attachments", ["uploaded_at"], unique=False)
    op.create_index(
        "ix_message_attachments_message_uploaded",
        "message_attachments",
        ["message_id", "uploaded_at"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_message_attachments_message_uploaded", table_name="message_attachments")
    op.drop_index("ix_message_attachments_uploaded_at", table_name="message_attachments")
    op.drop_index("ix_message_attachments_uploaded_by_id", table_name="message_attachments")
    op.drop_index("ix_message_attachments_message_id", table_name="message_attachments")
    op.drop_table("message_attachments")
