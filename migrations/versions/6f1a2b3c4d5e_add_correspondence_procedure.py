"""add correspondence procedural workflow

Revision ID: 6f1a2b3c4d5e
Revises: 5e0f1a2b3c4
Create Date: 2026-08-03 00:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = "6f1a2b3c4d5e"
down_revision = "5e0f1a2b3c4"
branch_labels = None
depends_on = None


def _add_procedure_columns(table_name: str, default_status: str) -> None:
    with op.batch_alter_table(table_name, schema=None) as batch_op:
        batch_op.add_column(sa.Column("status", sa.String(length=30), nullable=False, server_default=default_status))
        batch_op.add_column(sa.Column("route_mode", sa.String(length=30), nullable=False, server_default="DIRECT"))
        batch_op.add_column(sa.Column("mail_scope", sa.String(length=20), nullable=False, server_default="EXTERNAL"))
        batch_op.add_column(sa.Column("priority", sa.String(length=20), nullable=False, server_default="NORMAL"))
        batch_op.add_column(sa.Column("confidentiality", sa.String(length=20), nullable=False, server_default="NORMAL"))
        batch_op.add_column(sa.Column("due_date", sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column("current_target_kind", sa.String(length=30), nullable=True))
        batch_op.add_column(sa.Column("current_target_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("current_target_label", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("current_assignee_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("closed_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("closed_by_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("deadline_notified_on", sa.String(length=10), nullable=True))
        batch_op.create_foreign_key(
            f"fk_{table_name}_current_assignee",
            "users",
            ["current_assignee_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            f"fk_{table_name}_closed_by",
            "users",
            ["closed_by_id"],
            ["id"],
        )
        for column in (
            "status", "route_mode", "mail_scope", "priority", "confidentiality",
            "due_date", "current_target_kind", "current_target_id", "current_target_label",
            "current_assignee_id", "closed_at", "closed_by_id", "deadline_notified_on",
        ):
            batch_op.create_index(f"ix_{table_name}_{column}", [column], unique=False)


def upgrade() -> None:
    _add_procedure_columns("corr_inbound", "RECEIVED")
    _add_procedure_columns("corr_outbound", "DRAFT")

    op.create_table(
        "corr_movement",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("inbound_id", sa.Integer(), nullable=True),
        sa.Column("outbound_id", sa.Integer(), nullable=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("from_status", sa.String(length=30), nullable=True),
        sa.Column("to_status", sa.String(length=30), nullable=True),
        sa.Column("target_kind", sa.String(length=30), nullable=True),
        sa.Column("target_id", sa.Integer(), nullable=True),
        sa.Column("target_label", sa.String(length=255), nullable=True),
        sa.Column("target_user_id", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("is_internal", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "(inbound_id IS NOT NULL) OR (outbound_id IS NOT NULL)",
            name="ck_corr_movement_parent",
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["inbound_id"], ["corr_inbound.id"]),
        sa.ForeignKeyConstraint(["outbound_id"], ["corr_outbound.id"]),
        sa.ForeignKeyConstraint(["target_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "inbound_id", "outbound_id", "actor_user_id", "action", "from_status",
        "to_status", "target_kind", "target_id", "target_user_id", "is_internal", "created_at",
    ):
        op.create_index(f"ix_corr_movement_{column}", "corr_movement", [column], unique=False)
    op.create_index("ix_corr_movement_inbound_created", "corr_movement", ["inbound_id", "created_at"], unique=False)
    op.create_index("ix_corr_movement_outbound_created", "corr_movement", ["outbound_id", "created_at"], unique=False)

    with op.batch_alter_table("corr_attachment", schema=None) as batch_op:
        batch_op.add_column(sa.Column("movement_id", sa.Integer(), nullable=True))
        batch_op.create_index("ix_corr_attachment_movement_id", ["movement_id"], unique=False)
        batch_op.create_foreign_key(
            "fk_corr_attachment_movement",
            "corr_movement",
            ["movement_id"],
            ["id"],
        )


def _drop_procedure_columns(table_name: str) -> None:
    with op.batch_alter_table(table_name, schema=None) as batch_op:
        for column in reversed((
            "status", "route_mode", "mail_scope", "priority", "confidentiality",
            "due_date", "current_target_kind", "current_target_id", "current_target_label",
            "current_assignee_id", "closed_at", "closed_by_id", "deadline_notified_on",
        )):
            batch_op.drop_index(f"ix_{table_name}_{column}")
        batch_op.drop_constraint(f"fk_{table_name}_closed_by", type_="foreignkey")
        batch_op.drop_constraint(f"fk_{table_name}_current_assignee", type_="foreignkey")
        for column in reversed((
            "status", "route_mode", "mail_scope", "priority", "confidentiality",
            "due_date", "current_target_kind", "current_target_id", "current_target_label",
            "current_assignee_id", "closed_at", "closed_by_id", "deadline_notified_on",
        )):
            batch_op.drop_column(column)


def downgrade() -> None:
    with op.batch_alter_table("corr_attachment", schema=None) as batch_op:
        batch_op.drop_constraint("fk_corr_attachment_movement", type_="foreignkey")
        batch_op.drop_index("ix_corr_attachment_movement_id")
        batch_op.drop_column("movement_id")
    op.drop_table("corr_movement")
    _drop_procedure_columns("corr_outbound")
    _drop_procedure_columns("corr_inbound")
