"""add trouble ticket attachments

Revision ID: b7c8d9e0f1a2
Revises: a6b7c8d9e0f1
Create Date: 2026-08-18
"""

from alembic import op
import sqlalchemy as sa


revision = "b7c8d9e0f1a2"
down_revision = "a6b7c8d9e0f1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "trouble_ticket_attachments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticket_id", sa.Integer(), sa.ForeignKey("trouble_tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("stored_name", sa.String(length=255), nullable=False, unique=True),
        sa.Column("mime_type", sa.String(length=120), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("uploaded_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_trouble_ticket_attachments_ticket_id", "trouble_ticket_attachments", ["ticket_id"])
    op.create_index("ix_trouble_ticket_attachments_uploaded_by_id", "trouble_ticket_attachments", ["uploaded_by_id"])
    op.create_index("ix_trouble_ticket_attachments_uploaded_at", "trouble_ticket_attachments", ["uploaded_at"])


def downgrade():
    op.drop_table("trouble_ticket_attachments")
