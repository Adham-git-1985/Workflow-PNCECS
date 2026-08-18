"""add employee trouble tickets

Revision ID: a6b7c8d9e0f1
Revises: e5f6a7b8c9d0
Create Date: 2026-08-18
"""

from alembic import op
import sqlalchemy as sa


revision = "a6b7c8d9e0f1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "trouble_tickets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("requester_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("assigned_to_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("subject", sa.String(length=250), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False, server_default="OTHER"),
        sa.Column("priority", sa.String(length=20), nullable=False, server_default="NORMAL"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="OPEN"),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_trouble_ticket_requester_status", "trouble_tickets", ["requester_id", "status"])
    op.create_index("ix_trouble_ticket_assignee_status", "trouble_tickets", ["assigned_to_id", "status"])
    op.create_index("ix_trouble_tickets_category", "trouble_tickets", ["category"])
    op.create_index("ix_trouble_tickets_priority", "trouble_tickets", ["priority"])
    op.create_index("ix_trouble_tickets_status", "trouble_tickets", ["status"])
    op.create_index("ix_trouble_tickets_created_at", "trouble_tickets", ["created_at"])
    op.create_index("ix_trouble_tickets_updated_at", "trouble_tickets", ["updated_at"])

    op.create_table(
        "trouble_ticket_comments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticket_id", sa.Integer(), sa.ForeignKey("trouble_tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("author_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_trouble_ticket_comments_ticket_id", "trouble_ticket_comments", ["ticket_id"])
    op.create_index("ix_trouble_ticket_comments_author_id", "trouble_ticket_comments", ["author_id"])
    op.create_index("ix_trouble_ticket_comments_created_at", "trouble_ticket_comments", ["created_at"])


def downgrade():
    op.drop_table("trouble_ticket_comments")
    op.drop_table("trouble_tickets")
