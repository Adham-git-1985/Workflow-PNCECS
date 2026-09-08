"""add workflow list and notification polling indexes

Revision ID: g8f9a0b1c2d3
Revises: f7e8d9c0b1a2
Create Date: 2026-09-08
"""

from alembic import op
import sqlalchemy as sa


revision = "g8f9a0b1c2d3"
down_revision = "f7e8d9c0b1a2"
branch_labels = None
depends_on = None


INDEXES = {
    "workflow_request": (
        ("ix_workflow_request_status_id", ("status", "id")),
    ),
    "audit_log": (
        ("ix_audit_request_created", ("request_id", "created_at")),
        (
            "ix_audit_request_action_target",
            ("request_id", "action", "target_type", "target_id"),
        ),
    ),
    "workflow_instances": (
        (
            "ix_workflow_instances_open_current",
            ("is_completed", "current_step_order", "id"),
        ),
    ),
    "workflow_instance_steps": (
        (
            "ix_workflow_instance_steps_instance_status_order",
            ("instance_id", "status", "step_order"),
        ),
    ),
    "workflow_step_tasks": (
        (
            "ix_workflow_step_tasks_instance_step_status_user",
            ("instance_id", "step_order", "status", "assignee_user_id"),
        ),
        (
            "ix_workflow_step_tasks_request_status",
            ("request_id", "status"),
        ),
    ),
    "notification": (
        (
            "ix_notification_poll_unread",
            ("user_id", "is_mirror", "is_visible", "is_read", "source"),
        ),
        (
            "ix_notification_poll_latest",
            ("user_id", "is_mirror", "is_visible", "id"),
        ),
    ),
}


def upgrade():
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for table_name, definitions in INDEXES.items():
        if table_name not in tables:
            continue
        existing_indexes = {
            index["name"] for index in inspector.get_indexes(table_name)
        }
        for index_name, columns in definitions:
            if index_name not in existing_indexes:
                op.create_index(index_name, table_name, list(columns), unique=False)


def downgrade():
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for table_name, definitions in reversed(tuple(INDEXES.items())):
        if table_name not in tables:
            continue
        existing_indexes = {
            index["name"] for index in inspector.get_indexes(table_name)
        }
        for index_name, _columns in reversed(definitions):
            if index_name in existing_indexes:
                op.drop_index(index_name, table_name=table_name)
