"""add notification and work-queue performance indexes

Revision ID: zi8d9e0f1a2
Revises: zh7c8d9e0f1
Create Date: 2026-10-10 23:10:00
"""

from alembic import op
import sqlalchemy as sa


revision = "zi8d9e0f1a2"
down_revision = "zh7c8d9e0f1"
branch_labels = None
depends_on = None


INDEXES = {
    "delegations": (
        (
            "ix_delegations_to_active_window",
            ("to_user_id", "is_active", "starts_at", "expires_at"),
        ),
        (
            "ix_delegations_from_active_window",
            ("from_user_id", "is_active", "starts_at", "expires_at"),
        ),
    ),
    "notification": (
        (
            "ix_notification_admin_visible_users",
            ("source", "is_mirror", "is_visible", "user_id"),
        ),
        (
            "ix_notification_admin_user_latest",
            ("user_id", "source", "is_mirror", "is_visible", "created_at", "id"),
        ),
    ),
    "workflow_request": (
        (
            "ix_workflow_request_created_id",
            ("created_at", "id"),
        ),
    ),
    "hr_ss_request": (
        (
            "ix_hr_ss_request_status_id",
            ("status", "id"),
        ),
    ),
    "hr_ss_request_approval": (
        (
            "ix_hr_ss_approval_status_user_request",
            ("status", "approver_user_id", "request_id"),
        ),
    ),
    "employee_followup_reports": (
        (
            "ix_followup_employee_reminder_due",
            ("status", "period_end", "last_employee_reminder_at"),
        ),
        (
            "ix_followup_manager_reminder_due",
            ("status", "submitted_at", "last_manager_reminder_at"),
        ),
    ),
}

ROLE_INDEX = "ix_hr_ss_approval_status_role_norm_request"


def _index_names(bind, inspector, table_name: str) -> set[str]:
    if bind.dialect.name == "sqlite":
        return {
            str(row[0])
            for row in bind.execute(
                sa.text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'index' AND tbl_name = :table_name"
                ),
                {"table_name": table_name},
            )
            if row[0]
        }
    if bind.dialect.name == "postgresql":
        return {
            str(row[0])
            for row in bind.execute(
                sa.text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename = :table_name"
                ),
                {"table_name": table_name},
            )
            if row[0]
        }
    return {
        index["name"]
        for index in inspector.get_indexes(table_name)
        if index.get("name")
    }


def _has_expression_index(bind, inspector, index_name: str) -> bool:
    if bind.dialect.name == "sqlite":
        return bind.execute(
            sa.text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'index' AND name = :index_name"
            ),
            {"index_name": index_name},
        ).first() is not None
    if bind.dialect.name == "postgresql":
        return bind.execute(
            sa.text(
                "SELECT 1 FROM pg_indexes "
                "WHERE indexname = :index_name"
            ),
            {"index_name": index_name},
        ).first() is not None
    return index_name in _index_names(
        bind,
        inspector,
        "hr_ss_request_approval",
    )


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for table_name, definitions in INDEXES.items():
        if table_name not in tables:
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        existing = _index_names(bind, inspector, table_name)
        for index_name, index_columns in definitions:
            if index_name not in existing and set(index_columns).issubset(columns):
                op.create_index(index_name, table_name, list(index_columns), unique=False)

    if "hr_ss_request_approval" not in tables:
        return
    approval_columns = {
        column["name"]
        for column in inspector.get_columns("hr_ss_request_approval")
    }
    if (
        bind.dialect.name in {"sqlite", "postgresql"}
        and {"status", "approver_role", "request_id"}.issubset(approval_columns)
        and not _has_expression_index(bind, inspector, ROLE_INDEX)
    ):
        op.execute(sa.text(
            f"CREATE INDEX {ROLE_INDEX} "
            "ON hr_ss_request_approval "
            "(status, upper(replace(replace(replace(approver_role, '_', ''), "
            "'-', ''), ' ', '')), request_id)"
        ))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if (
        "hr_ss_request_approval" in tables
        and bind.dialect.name in {"sqlite", "postgresql"}
        and _has_expression_index(bind, inspector, ROLE_INDEX)
    ):
        op.execute(sa.text(f"DROP INDEX IF EXISTS {ROLE_INDEX}"))

    inspector = sa.inspect(bind)
    for table_name, definitions in reversed(tuple(INDEXES.items())):
        if table_name not in tables:
            continue
        existing = _index_names(bind, inspector, table_name)
        for index_name, _columns in reversed(definitions):
            if index_name in existing:
                op.drop_index(index_name, table_name=table_name)
