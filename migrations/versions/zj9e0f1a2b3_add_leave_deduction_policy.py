"""add configurable monthly leave-deduction policy

Revision ID: zj9e0f1a2b3
Revises: zi8d9e0f1a2
Create Date: 2026-10-15 12:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "zj9e0f1a2b3"
down_revision = "zi8d9e0f1a2"
branch_labels = None
depends_on = None


def _columns(inspector, table_name):
    try:
        return {column["name"] for column in inspector.get_columns(table_name)}
    except Exception:
        return set()


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "hr_att_deduction_override" not in tables:
        op.create_table(
            "hr_att_deduction_override",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("leave_type_id", sa.Integer(), sa.ForeignKey("hr_leave_type.id"), nullable=True),
            sa.Column("hours_per_day", sa.Float(), nullable=False, server_default="6.5"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        )
        op.create_index("ix_hr_att_deduction_override_user_id", "hr_att_deduction_override", ["user_id"])
        op.create_index("ix_hr_att_deduction_override_leave_type_id", "hr_att_deduction_override", ["leave_type_id"])
        op.create_index("ix_hr_att_deduction_override_created_at", "hr_att_deduction_override", ["created_at"])
        op.create_index("ix_hr_att_deduction_override_updated_at", "hr_att_deduction_override", ["updated_at"])

    additions = {
        "hr_att_deduction_config": {
            "source_selection_json": sa.Column("source_selection_json", sa.Text(), nullable=True, server_default='["DEPARTURES"]'),
            "leave_type_selection_json": sa.Column("leave_type_selection_json", sa.Text(), nullable=True),
            "automatic_enabled": sa.Column("automatic_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            "automatic_last_period": sa.Column("automatic_last_period", sa.String(length=7), nullable=True),
            "default_carry_departures": sa.Column("default_carry_departures", sa.Boolean(), nullable=False, server_default=sa.false()),
            "default_carry_late": sa.Column("default_carry_late", sa.Boolean(), nullable=False, server_default=sa.false()),
            "default_carry_early": sa.Column("default_carry_early", sa.Boolean(), nullable=False, server_default=sa.false()),
        },
        "hr_att_deduction_run": {
            "execution_mode": sa.Column("execution_mode", sa.String(length=24), nullable=False, server_default="MANUAL"),
            "operation_type": sa.Column("operation_type", sa.String(length=24), nullable=False, server_default="NORMAL"),
            "reversal_of_run_id": sa.Column("reversal_of_run_id", sa.Integer(), nullable=True),
            "replacement_of_run_id": sa.Column("replacement_of_run_id", sa.Integer(), nullable=True),
            "carry_departures": sa.Column("carry_departures", sa.Boolean(), nullable=False, server_default=sa.false()),
            "carry_late": sa.Column("carry_late", sa.Boolean(), nullable=False, server_default=sa.false()),
            "carry_early": sa.Column("carry_early", sa.Boolean(), nullable=False, server_default=sa.false()),
            "needs_review": sa.Column("needs_review", sa.Boolean(), nullable=False, server_default=sa.false()),
            "review_reason": sa.Column("review_reason", sa.Text(), nullable=True),
        },
        "hr_att_deduction_item": {
            "departure_minutes": sa.Column("departure_minutes", sa.Integer(), nullable=False, server_default="0"),
            "carried_departure_minutes": sa.Column("carried_departure_minutes", sa.Integer(), nullable=False, server_default="0"),
            "carried_late_minutes": sa.Column("carried_late_minutes", sa.Integer(), nullable=False, server_default="0"),
            "carried_early_leave_minutes": sa.Column("carried_early_leave_minutes", sa.Integer(), nullable=False, server_default="0"),
            "carry_departure_minutes": sa.Column("carry_departure_minutes", sa.Integer(), nullable=False, server_default="0"),
            "carry_late_minutes": sa.Column("carry_late_minutes", sa.Integer(), nullable=False, server_default="0"),
            "carry_early_leave_minutes": sa.Column("carry_early_leave_minutes", sa.Integer(), nullable=False, server_default="0"),
        },
    }
    for table_name, columns in additions.items():
        if table_name not in tables:
            continue
        current = _columns(inspector, table_name)
        for name, column in columns.items():
            if name not in current:
                op.add_column(table_name, column)

    # The automatic job may be alive in more than one web/worker process.
    # Keep the period idempotent at the database boundary as well as in the
    # application check.  Partial indexes are supported by the SQLite and
    # PostgreSQL installations used by the portal; other engines retain the
    # application-level guard.
    if "hr_att_deduction_run" in tables:
        current_indexes = {
            index.get("name")
            for index in sa.inspect(bind).get_indexes("hr_att_deduction_run")
            if index.get("name")
        }
        if "ix_hr_att_deduction_run_status_period" not in current_indexes:
            op.create_index(
                "ix_hr_att_deduction_run_status_period",
                "hr_att_deduction_run",
                ["status", "year", "month", "id"],
            )
        if bind.dialect.name in {"sqlite", "postgresql"}:
            op.execute(sa.text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_hr_att_deduction_auto_period "
                "ON hr_att_deduction_run (year, month) "
                "WHERE execution_mode = 'AUTO'"
            ))

    if "hr_att_deduction_item" in tables:
        current_indexes = {
            index.get("name")
            for index in sa.inspect(bind).get_indexes("hr_att_deduction_item")
            if index.get("name")
        }
        if "ix_hr_att_deduction_item_user_run" not in current_indexes:
            op.create_index(
                "ix_hr_att_deduction_item_user_run",
                "hr_att_deduction_item",
                ["user_id", "run_id"],
            )

    notification_columns = _columns(inspector, "notification") if "notification" in tables else set()
    if (
        "notification" in tables
        and {"user_id", "event_key", "is_mirror"}.issubset(notification_columns)
        and bind.dialect.name in {"sqlite", "postgresql"}
    ):
        op.execute(sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_hr_deduction_event "
            "ON notification (user_id, event_key, is_mirror) "
            "WHERE event_key LIKE 'hr-deduction:%'"
        ))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    notification_columns = _columns(inspector, "notification") if "notification" in tables else set()
    if (
        "notification" in tables
        and {"user_id", "event_key", "is_mirror"}.issubset(notification_columns)
        and bind.dialect.name in {"sqlite", "postgresql"}
    ):
        op.execute(sa.text("DROP INDEX IF EXISTS uq_notification_hr_deduction_event"))
    if "hr_att_deduction_run" in tables:
        op.execute(sa.text("DROP INDEX IF EXISTS uq_hr_att_deduction_auto_period"))
        if "ix_hr_att_deduction_run_status_period" in {
            index.get("name")
            for index in sa.inspect(bind).get_indexes("hr_att_deduction_run")
            if index.get("name")
        }:
            op.drop_index("ix_hr_att_deduction_run_status_period", table_name="hr_att_deduction_run")
    if "hr_att_deduction_item" in tables:
        if "ix_hr_att_deduction_item_user_run" in {
            index.get("name")
            for index in sa.inspect(bind).get_indexes("hr_att_deduction_item")
            if index.get("name")
        }:
            op.drop_index("ix_hr_att_deduction_item_user_run", table_name="hr_att_deduction_item")
    for table_name, names in {
        "hr_att_deduction_item": (
            "carry_early_leave_minutes", "carry_late_minutes", "carry_departure_minutes",
            "carried_early_leave_minutes", "carried_late_minutes", "carried_departure_minutes",
            "departure_minutes",
        ),
        "hr_att_deduction_run": (
            "review_reason", "needs_review", "carry_early", "carry_late", "carry_departures",
            "replacement_of_run_id", "reversal_of_run_id", "operation_type", "execution_mode",
        ),
        "hr_att_deduction_config": (
            "default_carry_early", "default_carry_late", "default_carry_departures",
            "automatic_last_period", "automatic_enabled", "leave_type_selection_json", "source_selection_json",
        ),
    }.items():
        if table_name not in tables:
            continue
        current = _columns(inspector, table_name)
        for name in names:
            if name in current:
                op.drop_column(table_name, name)
    if "hr_att_deduction_override" in tables:
        op.drop_index("ix_hr_att_deduction_override_updated_at", table_name="hr_att_deduction_override")
        op.drop_index("ix_hr_att_deduction_override_created_at", table_name="hr_att_deduction_override")
        op.drop_index("ix_hr_att_deduction_override_leave_type_id", table_name="hr_att_deduction_override")
        op.drop_index("ix_hr_att_deduction_override_user_id", table_name="hr_att_deduction_override")
        op.drop_table("hr_att_deduction_override")
