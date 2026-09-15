"""add employee achievement attachments and approval routing

Revision ID: k3c4d5e6f7g8
Revises: j3c4d5e6f7g8
Create Date: 2026-09-15
"""

from alembic import op
import sqlalchemy as sa


revision = "k3c4d5e6f7g8"
down_revision = "j3c4d5e6f7g8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "hr_employee_achievement_attachment",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("achievement_id", sa.Integer(), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("stored_name", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("uploaded_by_id", sa.Integer(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["achievement_id"], ["hr_employee_achievement.id"]),
        sa.ForeignKeyConstraint(["uploaded_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stored_name", name="uq_hr_achievement_attachment_stored_name"),
    )
    op.create_index(
        "ix_hr_employee_achievement_attachment_achievement_id",
        "hr_employee_achievement_attachment",
        ["achievement_id"],
    )
    op.create_index(
        "ix_hr_employee_achievement_attachment_uploaded_by_id",
        "hr_employee_achievement_attachment",
        ["uploaded_by_id"],
    )
    op.create_index(
        "ix_hr_employee_achievement_attachment_uploaded_at",
        "hr_employee_achievement_attachment",
        ["uploaded_at"],
    )
    op.create_index(
        "ix_hr_achievement_attachment_achievement",
        "hr_employee_achievement_attachment",
        ["achievement_id", "uploaded_at"],
    )

    op.create_table(
        "hr_employee_achievement_approval",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("achievement_id", sa.Integer(), nullable=False),
        sa.Column("approver_user_id", sa.Integer(), nullable=False),
        sa.Column("approver_role", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("distinction_level", sa.String(length=20), nullable=True),
        sa.Column("evaluation_points", sa.Float(), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("assigned_at", sa.DateTime(), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("viewed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "approver_role IN ('DIRECT_MANAGER','SECRETARY_GENERAL')",
            name="ck_hr_achievement_approval_role",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','APPROVED','REJECTED','SKIPPED','VIEW_ONLY')",
            name="ck_hr_achievement_approval_status",
        ),
        sa.CheckConstraint(
            "evaluation_points IS NULL OR (evaluation_points >= 0 AND evaluation_points <= 3)",
            name="ck_hr_achievement_approval_points",
        ),
        sa.ForeignKeyConstraint(["achievement_id"], ["hr_employee_achievement.id"]),
        sa.ForeignKeyConstraint(["approver_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "achievement_id",
            "approver_user_id",
            "approver_role",
            name="uq_hr_achievement_approval_recipient",
        ),
    )
    op.create_index(
        "ix_hr_employee_achievement_approval_achievement_id",
        "hr_employee_achievement_approval",
        ["achievement_id"],
    )
    op.create_index(
        "ix_hr_employee_achievement_approval_approver_user_id",
        "hr_employee_achievement_approval",
        ["approver_user_id"],
    )
    op.create_index(
        "ix_hr_employee_achievement_approval_approver_role",
        "hr_employee_achievement_approval",
        ["approver_role"],
    )
    op.create_index(
        "ix_hr_employee_achievement_approval_status",
        "hr_employee_achievement_approval",
        ["status"],
    )
    op.create_index(
        "ix_hr_achievement_approval_user_status",
        "hr_employee_achievement_approval",
        ["approver_user_id", "approver_role", "status"],
    )


def downgrade():
    op.drop_index("ix_hr_achievement_approval_user_status", table_name="hr_employee_achievement_approval")
    op.drop_index("ix_hr_employee_achievement_approval_status", table_name="hr_employee_achievement_approval")
    op.drop_index("ix_hr_employee_achievement_approval_approver_role", table_name="hr_employee_achievement_approval")
    op.drop_index("ix_hr_employee_achievement_approval_approver_user_id", table_name="hr_employee_achievement_approval")
    op.drop_index("ix_hr_employee_achievement_approval_achievement_id", table_name="hr_employee_achievement_approval")
    op.drop_table("hr_employee_achievement_approval")
    op.drop_index("ix_hr_achievement_attachment_achievement", table_name="hr_employee_achievement_attachment")
    op.drop_index("ix_hr_employee_achievement_attachment_uploaded_at", table_name="hr_employee_achievement_attachment")
    op.drop_index("ix_hr_employee_achievement_attachment_uploaded_by_id", table_name="hr_employee_achievement_attachment")
    op.drop_index("ix_hr_employee_achievement_attachment_achievement_id", table_name="hr_employee_achievement_attachment")
    op.drop_table("hr_employee_achievement_attachment")
