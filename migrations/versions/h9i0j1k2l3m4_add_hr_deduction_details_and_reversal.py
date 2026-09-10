"""add HR deduction exclusions, details, and reversal

Revision ID: h9i0j1k2l3m4
Revises: g8f9a0b1c2d3
Create Date: 2026-09-09
"""

from alembic import op
import sqlalchemy as sa


revision = "h9i0j1k2l3m4"
down_revision = "g8f9a0b1c2d3"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    adjustment_table = "hr_leave_balance_adjustment"
    if adjustment_table not in tables:
        op.create_table(
            adjustment_table,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("leave_type_id", sa.Integer(), nullable=False),
            sa.Column("year", sa.Integer(), nullable=False),
            sa.Column("days_delta", sa.Float(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("created_by_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["created_by_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["leave_type_id"], ["hr_leave_type.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    inspector = sa.inspect(bind)
    adjustment_indexes = {
        index["name"] for index in inspector.get_indexes(adjustment_table)
    }
    adjustment_index_specs = (
        ("ix_hr_leave_balance_adjustment_user_year_type", ["user_id", "year", "leave_type_id"]),
        ("ix_hr_leave_balance_adjustment_user_id", ["user_id"]),
        ("ix_hr_leave_balance_adjustment_leave_type_id", ["leave_type_id"]),
        ("ix_hr_leave_balance_adjustment_year", ["year"]),
        ("ix_hr_leave_balance_adjustment_created_at", ["created_at"]),
        ("ix_hr_leave_balance_adjustment_created_by_id", ["created_by_id"]),
    )
    for index_name, columns in adjustment_index_specs:
        if index_name not in adjustment_indexes:
            op.create_index(index_name, adjustment_table, columns)

    permission_columns = {
        column["name"] for column in inspector.get_columns("hr_permission_type")
    }
    if "deduct_from_allowance" not in permission_columns:
        with op.batch_alter_table("hr_permission_type") as batch:
            batch.add_column(
                sa.Column(
                    "deduct_from_allowance",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.true(),
                )
            )

    inspector = sa.inspect(bind)
    run_columns = {
        column["name"] for column in inspector.get_columns("hr_att_deduction_run")
    }
    run_indexes = {
        index["name"] for index in inspector.get_indexes("hr_att_deduction_run")
    }
    run_foreign_key_rows = inspector.get_foreign_keys("hr_att_deduction_run")
    has_reversed_by_foreign_key = any(
        "reversed_by_id" in (foreign_key.get("constrained_columns") or [])
        and foreign_key.get("referred_table") == "users"
        for foreign_key in run_foreign_key_rows
    )
    with op.batch_alter_table("hr_att_deduction_run") as batch:
        if "reversed_at" not in run_columns:
            batch.add_column(sa.Column("reversed_at", sa.DateTime(), nullable=True))
        if "reversed_by_id" not in run_columns:
            batch.add_column(sa.Column("reversed_by_id", sa.Integer(), nullable=True))
        if "reversal_note" not in run_columns:
            batch.add_column(sa.Column("reversal_note", sa.Text(), nullable=True))
        if not has_reversed_by_foreign_key:
            batch.create_foreign_key(
                "fk_hr_att_deduction_run_reversed_by",
                "users",
                ["reversed_by_id"],
                ["id"],
            )
        if "ix_hr_att_deduction_run_reversed_at" not in run_indexes:
            batch.create_index("ix_hr_att_deduction_run_reversed_at", ["reversed_at"])
        if "ix_hr_att_deduction_run_reversed_by_id" not in run_indexes:
            batch.create_index("ix_hr_att_deduction_run_reversed_by_id", ["reversed_by_id"])

    inspector = sa.inspect(bind)
    item_columns = {
        column["name"] for column in inspector.get_columns("hr_att_deduction_item")
    }
    if "details_json" not in item_columns:
        with op.batch_alter_table("hr_att_deduction_item") as batch:
            batch.add_column(sa.Column("details_json", sa.Text(), nullable=True))

    op.execute(
        "UPDATE hr_permission_type SET deduct_from_allowance=FALSE "
        "WHERE UPPER(COALESCE(code, '')) IN ('S', 'SICK', 'SICK_LEAVE', 'MEDICAL') "
        "OR LOWER(COALESCE(code, '')) LIKE '%sick%' "
        "OR COALESCE(name_ar, '') LIKE '%مرضي%' "
        "OR COALESCE(name_ar, '') LIKE '%مرضية%'"
    )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "hr_att_deduction_item" in tables:
        item_columns = {
            column["name"] for column in inspector.get_columns("hr_att_deduction_item")
        }
        if "details_json" in item_columns:
            with op.batch_alter_table("hr_att_deduction_item") as batch:
                batch.drop_column("details_json")

    inspector = sa.inspect(bind)
    if "hr_att_deduction_run" in tables:
        run_columns = {
            column["name"] for column in inspector.get_columns("hr_att_deduction_run")
        }
        run_indexes = {
            index["name"] for index in inspector.get_indexes("hr_att_deduction_run")
        }
        run_foreign_keys = {
            foreign_key["name"] for foreign_key in inspector.get_foreign_keys("hr_att_deduction_run")
        }
        with op.batch_alter_table("hr_att_deduction_run") as batch:
            if "ix_hr_att_deduction_run_reversed_by_id" in run_indexes:
                batch.drop_index("ix_hr_att_deduction_run_reversed_by_id")
            if "ix_hr_att_deduction_run_reversed_at" in run_indexes:
                batch.drop_index("ix_hr_att_deduction_run_reversed_at")
            if "fk_hr_att_deduction_run_reversed_by" in run_foreign_keys:
                batch.drop_constraint("fk_hr_att_deduction_run_reversed_by", type_="foreignkey")
            if "reversal_note" in run_columns:
                batch.drop_column("reversal_note")
            if "reversed_by_id" in run_columns:
                batch.drop_column("reversed_by_id")
            if "reversed_at" in run_columns:
                batch.drop_column("reversed_at")

    inspector = sa.inspect(bind)
    if "hr_permission_type" in tables:
        permission_columns = {
            column["name"] for column in inspector.get_columns("hr_permission_type")
        }
        if "deduct_from_allowance" in permission_columns:
            with op.batch_alter_table("hr_permission_type") as batch:
                batch.drop_column("deduct_from_allowance")

    inspector = sa.inspect(bind)
    if "hr_leave_balance_adjustment" in inspector.get_table_names():
        op.drop_table("hr_leave_balance_adjustment")
