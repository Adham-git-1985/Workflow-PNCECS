"""add employee achievements

Revision ID: i2b3c4d5e6f7
Revises: h1a2b3c4d5e6
Create Date: 2026-09-14
"""

from alembic import op
import sqlalchemy as sa


revision = "i2b3c4d5e6f7"
down_revision = "h1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "hr_employee_achievement",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("achievement_type", sa.String(length=30), nullable=False),
        sa.Column("title", sa.String(length=250), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("achieved_on", sa.String(length=10), nullable=False),
        sa.Column("issuer", sa.String(length=250), nullable=True),
        sa.Column("evidence_reference", sa.String(length=500), nullable=True),
        sa.Column("distinction_level", sa.String(length=20), nullable=False),
        sa.Column("evaluation_points", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("submitted_by_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_by_id", sa.Integer(), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "achievement_type IN ('AWARD','RESEARCH','INNOVATION','DEVELOPMENT','COMMUNITY','OTHER')",
            name="ck_hr_employee_achievement_type",
        ),
        sa.CheckConstraint(
            "distinction_level IN ('NOTABLE','SIGNIFICANT','EXCEPTIONAL')",
            name="ck_hr_employee_achievement_level",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','APPROVED','REJECTED')",
            name="ck_hr_employee_achievement_status",
        ),
        sa.CheckConstraint(
            "evaluation_points >= 0 AND evaluation_points <= 3",
            name="ck_hr_employee_achievement_points",
        ),
        sa.ForeignKeyConstraint(["reviewed_by_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["submitted_by_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_hr_employee_achievement_user_id", "hr_employee_achievement", ["user_id"])
    op.create_index("ix_hr_employee_achievement_type", "hr_employee_achievement", ["achievement_type"])
    op.create_index("ix_hr_employee_achievement_achieved_on", "hr_employee_achievement", ["achieved_on"])
    op.create_index("ix_hr_employee_achievement_level", "hr_employee_achievement", ["distinction_level"])
    op.create_index("ix_hr_employee_achievement_status", "hr_employee_achievement", ["status"])
    op.create_index("ix_hr_employee_achievement_submitted_by_id", "hr_employee_achievement", ["submitted_by_id"])
    op.create_index("ix_hr_employee_achievement_reviewed_by_id", "hr_employee_achievement", ["reviewed_by_id"])
    op.create_index("ix_hr_employee_achievement_created_at", "hr_employee_achievement", ["created_at"])
    op.create_index(
        "ix_hr_achievement_user_date_status",
        "hr_employee_achievement",
        ["user_id", "achieved_on", "status"],
    )


def downgrade():
    op.drop_index("ix_hr_achievement_user_date_status", table_name="hr_employee_achievement")
    op.drop_index("ix_hr_employee_achievement_created_at", table_name="hr_employee_achievement")
    op.drop_index("ix_hr_employee_achievement_reviewed_by_id", table_name="hr_employee_achievement")
    op.drop_index("ix_hr_employee_achievement_submitted_by_id", table_name="hr_employee_achievement")
    op.drop_index("ix_hr_employee_achievement_status", table_name="hr_employee_achievement")
    op.drop_index("ix_hr_employee_achievement_level", table_name="hr_employee_achievement")
    op.drop_index("ix_hr_employee_achievement_achieved_on", table_name="hr_employee_achievement")
    op.drop_index("ix_hr_employee_achievement_type", table_name="hr_employee_achievement")
    op.drop_index("ix_hr_employee_achievement_user_id", table_name="hr_employee_achievement")
    op.drop_table("hr_employee_achievement")
