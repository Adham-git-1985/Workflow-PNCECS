"""convert attendance schedules to weekly HR-owned workflow

Revision ID: a4b5c6d7e8f9
Revises: z3a4b5c6d7e8
Create Date: 2026-09-20
"""

from alembic import op
import sqlalchemy as sa


revision = "a4b5c6d7e8f9"
down_revision = "z3a4b5c6d7e8"
branch_labels = None
depends_on = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return any(
        column.get("name") == column_name
        for column in inspector.get_columns(table_name)
    )


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    return any(
        index.get("name") == index_name
        for index in inspector.get_indexes(table_name)
    )


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("hr_attendance_schedule_plan"):
        return

    columns = (
        ("general_director_user_id", sa.Integer(), True),
        ("request_type", sa.String(length=24), False),
        ("general_director_approved_at", sa.DateTime(), True),
        ("general_director_approved_by_id", sa.Integer(), True),
        ("admin_approved_at", sa.DateTime(), True),
        ("admin_approved_by_id", sa.Integer(), True),
    )
    for name, column_type, nullable in columns:
        if _has_column(inspector, "hr_attendance_schedule_plan", name):
            continue
        default = "'BASELINE'" if name == "request_type" else None
        op.add_column(
            "hr_attendance_schedule_plan",
            sa.Column(
                name,
                column_type,
                nullable=nullable,
                server_default=default,
            ),
        )

    # Existing rows predate the distinction between baseline schedules and
    # employee requests.  Treat them as baseline rows, then remove the server
    # default so future writes use the model default.
    op.execute(
        sa.text(
            "UPDATE hr_attendance_schedule_plan "
            "SET request_type = 'BASELINE' "
            "WHERE request_type IS NULL OR request_type = ''"
        )
    )
    try:
        op.alter_column(
            "hr_attendance_schedule_plan",
            "request_type",
            server_default=None,
        )
    except Exception:
        # SQLite deployments may not support this alteration; the runtime
        # schema synchronizer still supplies the model-level default.
        pass

    inspector = sa.inspect(bind)
    for index_name, columns_for_index in (
        (
            "ix_hr_att_schedule_plan_general_director_user_id",
            ["general_director_user_id"],
        ),
        ("ix_hr_att_schedule_plan_request_type", ["request_type"]),
        (
            "ix_hr_att_schedule_period_request_type",
            ["period_start", "request_type"],
        ),
    ):
        if not _has_index(inspector, "hr_attendance_schedule_plan", index_name):
            op.create_index(
                index_name,
                "hr_attendance_schedule_plan",
                columns_for_index,
            )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("hr_attendance_schedule_plan"):
        return
    for index_name in (
        "ix_hr_att_schedule_period_request_type",
        "ix_hr_att_schedule_plan_request_type",
        "ix_hr_att_schedule_plan_general_director_user_id",
    ):
        if _has_index(inspector, "hr_attendance_schedule_plan", index_name):
            op.drop_index(index_name, table_name="hr_attendance_schedule_plan")
    for column_name in (
        "admin_approved_by_id",
        "admin_approved_at",
        "general_director_approved_by_id",
        "general_director_approved_at",
        "request_type",
        "general_director_user_id",
    ):
        if _has_column(inspector, "hr_attendance_schedule_plan", column_name):
            op.drop_column("hr_attendance_schedule_plan", column_name)
