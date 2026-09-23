"""allow multiple scheduled attendance report emails per day

Revision ID: za1b2c3d4e5f
Revises: z9a0b1c2d3e4
Create Date: 2026-09-23
"""

from alembic import op
import sqlalchemy as sa


revision = "za1b2c3d4e5f"
down_revision = "z9a0b1c2d3e4"
branch_labels = None
depends_on = None


TABLE = "hr_attendance_email_report_delivery"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(TABLE):
        return

    columns = {column["name"] for column in inspector.get_columns(TABLE)}
    if "scheduled_time" not in columns:
        with op.batch_alter_table(TABLE) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "scheduled_time",
                    sa.String(length=5),
                    nullable=False,
                    server_default="09:30",
                )
            )

    # The first version used UNIQUE(run_date).  Batch mode rebuilds SQLite's
    # table safely while retaining existing deliveries as the 09:30 slot.
    unique_constraints = {
        constraint.get("name")
        for constraint in sa.inspect(bind).get_unique_constraints(TABLE)
    }
    if "uq_hr_attendance_email_report_delivery_run_date" in unique_constraints:
        with op.batch_alter_table(TABLE) as batch_op:
            batch_op.drop_constraint(
                "uq_hr_attendance_email_report_delivery_run_date",
                type_="unique",
            )
            batch_op.create_unique_constraint(
                "uq_hr_attendance_email_report_delivery_run_slot",
                ["run_date", "scheduled_time"],
            )
    elif "uq_hr_attendance_email_report_delivery_run_slot" not in unique_constraints:
        with op.batch_alter_table(TABLE) as batch_op:
            batch_op.create_unique_constraint(
                "uq_hr_attendance_email_report_delivery_run_slot",
                ["run_date", "scheduled_time"],
            )

    index_names = {
        index.get("name")
        for index in sa.inspect(bind).get_indexes(TABLE)
    }
    if "ix_hr_attendance_email_report_delivery_scheduled_time" not in index_names:
        op.create_index(
            "ix_hr_attendance_email_report_delivery_scheduled_time",
            TABLE,
            ["scheduled_time"],
            unique=False,
        )


def downgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return

    op.drop_index(
        "ix_hr_attendance_email_report_delivery_scheduled_time",
        table_name=TABLE,
    )
    with op.batch_alter_table(TABLE) as batch_op:
        batch_op.drop_constraint(
            "uq_hr_attendance_email_report_delivery_run_slot",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_hr_attendance_email_report_delivery_run_date",
            ["run_date"],
        )
        batch_op.drop_column("scheduled_time")
