"""add meeting-scoped edit permissions and official minutes file

Revision ID: a4c5d6e7f8a9
Revises: 9c4d5e6f7081
Create Date: 2026-10-28 00:00:00

"""

from alembic import op
import sqlalchemy as sa


revision = "a4c5d6e7f8a9"
down_revision = "9c4d5e6f7081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "portal_meeting_permissions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("meeting_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("permission", sa.String(length=80), nullable=False),
        sa.Column("granted_by_user_id", sa.Integer(), nullable=True),
        sa.Column(
            "granted_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["meeting_id"], ["portal_meetings.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["granted_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "meeting_id",
            "user_id",
            "permission",
            name="uq_portal_meeting_permission",
        ),
    )
    op.create_index(
        "ix_portal_meeting_permissions_meeting",
        "portal_meeting_permissions",
        ["meeting_id"],
        unique=False,
    )
    op.create_index(
        "ix_portal_meeting_permissions_user",
        "portal_meeting_permissions",
        ["user_id"],
        unique=False,
    )

    op.create_table(
        "portal_meeting_minutes_files",
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
        sa.UniqueConstraint("meeting_id"),
        sa.UniqueConstraint("stored_name"),
    )
    op.create_index(
        "ix_portal_meeting_minutes_files_meeting_id",
        "portal_meeting_minutes_files",
        ["meeting_id"],
        unique=True,
    )
    op.create_index(
        "ix_portal_meeting_minutes_files_uploaded_by_user_id",
        "portal_meeting_minutes_files",
        ["uploaded_by_user_id"],
        unique=False,
    )

    # Existing organizers already have the same effective permission through
    # the application fallback.  Materialize it so the grant is visible and
    # remains explicit after future permission checks change.
    bind = op.get_bind()
    bind.execute(sa.text(
        """
        INSERT INTO portal_meeting_permissions
            (meeting_id, user_id, permission, granted_by_user_id, granted_at)
        SELECT m.id, m.created_by_user_id, 'PORTAL_MEETING_EDIT',
               m.created_by_user_id, CURRENT_TIMESTAMP
        FROM portal_meetings AS m
        WHERE m.created_by_user_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM portal_meeting_permissions AS p
              WHERE p.meeting_id = m.id
                AND p.user_id = m.created_by_user_id
                AND p.permission = 'PORTAL_MEETING_EDIT'
          )
        """
    ))


def downgrade() -> None:
    op.drop_index(
        "ix_portal_meeting_minutes_files_uploaded_by_user_id",
        table_name="portal_meeting_minutes_files",
    )
    op.drop_index(
        "ix_portal_meeting_minutes_files_meeting_id",
        table_name="portal_meeting_minutes_files",
    )
    op.drop_table("portal_meeting_minutes_files")

    op.drop_index(
        "ix_portal_meeting_permissions_user",
        table_name="portal_meeting_permissions",
    )
    op.drop_index(
        "ix_portal_meeting_permissions_meeting",
        table_name="portal_meeting_permissions",
    )
    op.drop_table("portal_meeting_permissions")
