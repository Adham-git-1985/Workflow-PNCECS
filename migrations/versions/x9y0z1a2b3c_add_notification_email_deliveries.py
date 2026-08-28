"""add notification email delivery outbox

Revision ID: x9y0z1a2b3c
Revises: w8x9y0z1a2b
Create Date: 2026-08-28 13:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "x9y0z1a2b3c"
down_revision = "w8x9y0z1a2b"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "notification",
        sa.Column(
            "email_delivery_mode",
            sa.String(length=30),
            nullable=False,
            server_default="GENERAL",
        ),
    )
    op.create_table(
        "notification_email_deliveries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("notification_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["notification_id"], ["notification.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("notification_id", name="uq_notification_email_delivery_notification"),
    )
    op.create_index(
        "ix_notification_email_deliveries_notification_id",
        "notification_email_deliveries",
        ["notification_id"],
    )
    op.create_index(
        "ix_notification_email_deliveries_user_id",
        "notification_email_deliveries",
        ["user_id"],
    )
    op.create_index(
        "ix_notification_email_deliveries_status",
        "notification_email_deliveries",
        ["status"],
    )
    op.create_index(
        "ix_notification_email_deliveries_next_attempt_at",
        "notification_email_deliveries",
        ["next_attempt_at"],
    )
    op.create_index(
        "ix_notification_email_delivery_pending",
        "notification_email_deliveries",
        ["status", "next_attempt_at", "created_at"],
    )


def downgrade():
    op.drop_index("ix_notification_email_delivery_pending", table_name="notification_email_deliveries")
    op.drop_index("ix_notification_email_deliveries_next_attempt_at", table_name="notification_email_deliveries")
    op.drop_index("ix_notification_email_deliveries_status", table_name="notification_email_deliveries")
    op.drop_index("ix_notification_email_deliveries_user_id", table_name="notification_email_deliveries")
    op.drop_index("ix_notification_email_deliveries_notification_id", table_name="notification_email_deliveries")
    op.drop_table("notification_email_deliveries")
    op.drop_column("notification", "email_delivery_mode")
