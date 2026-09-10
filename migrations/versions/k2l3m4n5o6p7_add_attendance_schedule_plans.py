"""add two-week attendance schedule plans

Revision ID: k2l3m4n5o6p7
Revises: j1k2l3m4n5o6
Create Date: 2026-09-10
"""

from alembic import op
import sqlalchemy as sa


revision = "k2l3m4n5o6p7"
down_revision = "j1k2l3m4n5o6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "hr_attendance_schedule_plan",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("manager_user_id", sa.Integer(), nullable=True),
        sa.Column("period_start", sa.String(length=10), nullable=False),
        sa.Column("period_end", sa.String(length=10), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("replaces_plan_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="DRAFT"),
        sa.Column("employee_note", sa.Text(), nullable=True),
        sa.Column("manager_note", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("manager_approved_at", sa.DateTime(), nullable=True),
        sa.Column("manager_approved_by_id", sa.Integer(), nullable=True),
        sa.Column("final_approved_at", sa.DateTime(), nullable=True),
        sa.Column("final_approved_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("updated_by_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["final_approved_by_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["manager_approved_by_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["manager_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["replaces_plan_id"],
            ["hr_attendance_schedule_plan.id"],
        ),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "period_start",
            "version_no",
            name="uq_hr_att_schedule_user_period_version",
        ),
    )
    op.create_index(
        "ix_hr_att_schedule_plan_user_id",
        "hr_attendance_schedule_plan",
        ["user_id"],
    )
    op.create_index(
        "ix_hr_att_schedule_plan_manager_user_id",
        "hr_attendance_schedule_plan",
        ["manager_user_id"],
    )
    op.create_index(
        "ix_hr_att_schedule_plan_period_start",
        "hr_attendance_schedule_plan",
        ["period_start"],
    )
    op.create_index(
        "ix_hr_att_schedule_plan_period_end",
        "hr_attendance_schedule_plan",
        ["period_end"],
    )
    op.create_index(
        "ix_hr_att_schedule_plan_replaces_plan_id",
        "hr_attendance_schedule_plan",
        ["replaces_plan_id"],
    )
    op.create_index(
        "ix_hr_att_schedule_plan_status",
        "hr_attendance_schedule_plan",
        ["status"],
    )
    op.create_index(
        "ix_hr_att_schedule_period_status",
        "hr_attendance_schedule_plan",
        ["period_start", "status"],
    )

    op.create_table(
        "hr_attendance_schedule_day",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("work_date", sa.String(length=10), nullable=False),
        sa.Column("day_type", sa.String(length=20), nullable=False, server_default="WORK"),
        sa.Column("schedule_id", sa.Integer(), nullable=True),
        sa.Column("start_time", sa.String(length=5), nullable=True),
        sa.Column("end_time", sa.String(length=5), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("edit_source", sa.String(length=24), nullable=False, server_default="EMPLOYEE"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("updated_by_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["hr_attendance_schedule_plan.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["schedule_id"], ["work_schedule.id"]),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", "work_date", name="uq_hr_att_schedule_plan_day"),
    )
    op.create_index(
        "ix_hr_att_schedule_day_plan_id",
        "hr_attendance_schedule_day",
        ["plan_id"],
    )
    op.create_index(
        "ix_hr_att_schedule_day_work_date",
        "hr_attendance_schedule_day",
        ["work_date"],
    )
    op.create_index(
        "ix_hr_att_schedule_day_date_type",
        "hr_attendance_schedule_day",
        ["work_date", "day_type"],
    )


def downgrade():
    op.drop_table("hr_attendance_schedule_day")
    op.drop_table("hr_attendance_schedule_plan")
