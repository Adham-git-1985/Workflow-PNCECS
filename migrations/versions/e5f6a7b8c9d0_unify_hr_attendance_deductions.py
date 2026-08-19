"""unify HR attendance grace, hybrid policy, and deduction approval

Revision ID: e5f6a7b8c9d0
Revises: c3d4e5f6a7b8
Create Date: 2026-08-18

"""

from alembic import op
import sqlalchemy as sa


revision = "e5f6a7b8c9d0"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("work_schedule") as batch:
        batch.add_column(sa.Column("start_grace_minutes", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("end_grace_minutes", sa.Integer(), nullable=True))

    with op.batch_alter_table("work_policy") as batch:
        batch.add_column(
            sa.Column(
                "hybrid_selection_mode",
                sa.String(length=20),
                nullable=False,
                server_default="FLEXIBLE",
            )
        )
        batch.add_column(sa.Column("hybrid_fixed_days_mask", sa.Integer(), nullable=True))

    with op.batch_alter_table("hr_att_deduction_config") as batch:
        batch.add_column(
            sa.Column(
                "permission_allowance_hours",
                sa.Float(),
                nullable=False,
                server_default="6",
            )
        )
        batch.add_column(sa.Column("annual_leave_type_id", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column(
                "deduction_sequence",
                sa.String(length=40),
                nullable=False,
                server_default="LEAVE_THEN_SALARY",
            )
        )
        batch.add_column(
            sa.Column(
                "require_approval",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
        batch.create_foreign_key(
            "fk_hr_att_deduction_config_annual_leave_type",
            "hr_leave_type",
            ["annual_leave_type_id"],
            ["id"],
        )
        batch.create_index(
            "ix_hr_att_deduction_config_annual_leave_type_id",
            ["annual_leave_type_id"],
        )

    with op.batch_alter_table("hr_att_deduction_run") as batch:
        batch.add_column(sa.Column("config_snapshot_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("approved_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("approved_by_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("approval_note", sa.Text(), nullable=True))
        batch.create_foreign_key(
            "fk_hr_att_deduction_run_approved_by",
            "users",
            ["approved_by_id"],
            ["id"],
        )
        batch.create_index("ix_hr_att_deduction_run_approved_at", ["approved_at"])
        batch.create_index("ix_hr_att_deduction_run_approved_by_id", ["approved_by_id"])

    with op.batch_alter_table("hr_att_deduction_item") as batch:
        batch.add_column(
            sa.Column("approved_permission_minutes", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("permission_allowance_minutes", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("excluded_minutes", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("chargeable_minutes", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("deduction_leave_type_id", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("leave_deduction_days", sa.Float(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("salary_deduction_days", sa.Float(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("remainder_minutes", sa.Integer(), nullable=False, server_default="0"))
        batch.create_foreign_key(
            "fk_hr_att_deduction_item_leave_type",
            "hr_leave_type",
            ["deduction_leave_type_id"],
            ["id"],
        )
        batch.create_index(
            "ix_hr_att_deduction_item_deduction_leave_type_id",
            ["deduction_leave_type_id"],
        )

    # Preserve the old single-grace behavior for existing templates.
    op.execute(
        "UPDATE work_schedule "
        "SET start_grace_minutes = grace_minutes, end_grace_minutes = grace_minutes "
        "WHERE start_grace_minutes IS NULL OR end_grace_minutes IS NULL"
    )
    op.execute(
        "UPDATE hr_att_deduction_item "
        "SET salary_deduction_days = amount "
        "WHERE amount IS NOT NULL AND amount > 0"
    )


def downgrade():
    with op.batch_alter_table("hr_att_deduction_item") as batch:
        batch.drop_index("ix_hr_att_deduction_item_deduction_leave_type_id")
        batch.drop_constraint("fk_hr_att_deduction_item_leave_type", type_="foreignkey")
        batch.drop_column("remainder_minutes")
        batch.drop_column("salary_deduction_days")
        batch.drop_column("leave_deduction_days")
        batch.drop_column("deduction_leave_type_id")
        batch.drop_column("chargeable_minutes")
        batch.drop_column("excluded_minutes")
        batch.drop_column("permission_allowance_minutes")
        batch.drop_column("approved_permission_minutes")

    with op.batch_alter_table("hr_att_deduction_run") as batch:
        batch.drop_index("ix_hr_att_deduction_run_approved_by_id")
        batch.drop_index("ix_hr_att_deduction_run_approved_at")
        batch.drop_constraint("fk_hr_att_deduction_run_approved_by", type_="foreignkey")
        batch.drop_column("approval_note")
        batch.drop_column("approved_by_id")
        batch.drop_column("approved_at")
        batch.drop_column("config_snapshot_json")

    with op.batch_alter_table("hr_att_deduction_config") as batch:
        batch.drop_index("ix_hr_att_deduction_config_annual_leave_type_id")
        batch.drop_constraint("fk_hr_att_deduction_config_annual_leave_type", type_="foreignkey")
        batch.drop_column("require_approval")
        batch.drop_column("deduction_sequence")
        batch.drop_column("annual_leave_type_id")
        batch.drop_column("permission_allowance_hours")

    with op.batch_alter_table("work_policy") as batch:
        batch.drop_column("hybrid_fixed_days_mask")
        batch.drop_column("hybrid_selection_mode")

    with op.batch_alter_table("work_schedule") as batch:
        batch.drop_column("end_grace_minutes")
        batch.drop_column("start_grace_minutes")
