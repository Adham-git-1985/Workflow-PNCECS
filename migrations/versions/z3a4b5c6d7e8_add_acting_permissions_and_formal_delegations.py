"""add scoped acting permissions and formal delegations

Revision ID: z3a4b5c6d7e8
Revises: z2a3b4c5d6e7
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa


revision = "z3a4b5c6d7e8"
down_revision = "z2a3b4c5d6e7"
branch_labels = None
depends_on = None


def _columns(bind, table_name):
    inspector = sa.inspect(bind)
    if not inspector.has_table(table_name):
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("acting_permissions"):
        op.create_table(
            "acting_permissions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("principal_user_id", sa.Integer(), nullable=False),
            sa.Column("acting_user_id", sa.Integer(), nullable=False),
            sa.Column("module_id", sa.String(length=120), nullable=True),
            sa.Column("module_name", sa.String(length=120), nullable=True),
            sa.Column("scope_type", sa.String(length=50), nullable=True),
            sa.Column("scope_id", sa.String(length=255), nullable=True),
            sa.Column("transaction_type", sa.String(length=120), nullable=True),
            sa.Column("request_type", sa.String(length=120), nullable=True),
            sa.Column("can_view", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("can_create", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("can_edit", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("can_forward", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("can_follow_up", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("can_reject", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("can_cancel", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("can_close", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("can_reopen", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("can_approve", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("start_at", sa.DateTime(), nullable=False),
            sa.Column("end_at", sa.DateTime(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="ACTIVE"),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("reference_number", sa.String(length=120), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_by", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("revoked_by", sa.Integer(), nullable=True),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.CheckConstraint(
                "principal_user_id <> acting_user_id",
                name="ck_acting_permissions_not_self",
            ),
            sa.ForeignKeyConstraint(["principal_user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["acting_user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
            sa.ForeignKeyConstraint(["revoked_by"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    acting_columns = _columns(bind, "acting_permissions")
    if acting_columns and "can_reopen" not in acting_columns:
        with op.batch_alter_table("acting_permissions") as batch_op:
            batch_op.add_column(
                sa.Column("can_reopen", sa.Boolean(), nullable=False, server_default=sa.false())
            )

    if not inspector.has_table("formal_delegations"):
        op.create_table(
            "formal_delegations",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("delegator_user_id", sa.Integer(), nullable=False),
            sa.Column("delegate_user_id", sa.Integer(), nullable=False),
            sa.Column("delegation_type", sa.String(length=80), nullable=False),
            sa.Column("module_id", sa.String(length=120), nullable=True),
            sa.Column("module_name", sa.String(length=120), nullable=True),
            sa.Column("scope_type", sa.String(length=50), nullable=True),
            sa.Column("scope_id", sa.String(length=255), nullable=True),
            sa.Column("transaction_type", sa.String(length=120), nullable=True),
            sa.Column("request_type", sa.String(length=120), nullable=True),
            sa.Column("legal_reference", sa.Text(), nullable=True),
            sa.Column("decision_number", sa.String(length=120), nullable=True),
            sa.Column("decision_date", sa.Date(), nullable=True),
            sa.Column("issuing_authority", sa.String(length=255), nullable=True),
            sa.Column("attachment_id", sa.Integer(), nullable=True),
            sa.Column("start_at", sa.DateTime(), nullable=False),
            sa.Column("end_at", sa.DateTime(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="ACTIVE"),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("allow_redelegation", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_by", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("revoked_by", sa.Integer(), nullable=True),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.CheckConstraint(
                "delegator_user_id <> delegate_user_id",
                name="ck_formal_delegations_not_self",
            ),
            sa.ForeignKeyConstraint(["delegator_user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["delegate_user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["attachment_id"], ["archived_file.id"]),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
            sa.ForeignKeyConstraint(["revoked_by"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    audit_columns = _columns(bind, "audit_log")
    if audit_columns:
        for name, column in (
            ("actual_user_id", sa.Column("actual_user_id", sa.Integer(), nullable=True)),
            ("acting_for_user_id", sa.Column("acting_for_user_id", sa.Integer(), nullable=True)),
            ("acting_permission_id", sa.Column("acting_permission_id", sa.Integer(), nullable=True)),
            ("formal_delegation_id", sa.Column("formal_delegation_id", sa.Integer(), nullable=True)),
            ("execution_context", sa.Column("execution_context", sa.String(length=30), nullable=True)),
            ("action_type", sa.Column("action_type", sa.String(length=100), nullable=True)),
            ("module_name", sa.Column("module_name", sa.String(length=120), nullable=True)),
            ("object_type", sa.Column("object_type", sa.String(length=80), nullable=True)),
            ("object_id", sa.Column("object_id", sa.Integer(), nullable=True)),
            ("ip_address", sa.Column("ip_address", sa.String(length=64), nullable=True)),
            ("user_agent", sa.Column("user_agent", sa.String(length=500), nullable=True)),
        ):
            if name not in audit_columns:
                with op.batch_alter_table("audit_log") as batch_op:
                    batch_op.add_column(column)
                audit_columns.add(name)

    for table, name, columns in (
        ("acting_permissions", "ix_acting_permissions_actor_window", ["acting_user_id", "status", "start_at", "end_at"]),
        ("acting_permissions", "ix_acting_permissions_principal_window", ["principal_user_id", "status", "start_at", "end_at"]),
        ("formal_delegations", "ix_formal_delegations_delegate_window", ["delegate_user_id", "status", "start_at", "end_at"]),
        ("formal_delegations", "ix_formal_delegations_delegator_window", ["delegator_user_id", "status", "start_at", "end_at"]),
        ("audit_log", "ix_audit_actual_user_created", ["actual_user_id", "created_at"]),
        ("audit_log", "ix_audit_acting_for_created", ["acting_for_user_id", "created_at"]),
        ("audit_log", "ix_audit_execution_context_created", ["execution_context", "created_at"]),
        ("audit_log", "ix_audit_formal_delegation", ["formal_delegation_id", "created_at"]),
    ):
        # Re-inspect after creating a table; SQLAlchemy's Inspector caches the
        # pre-upgrade table list.
        fresh_inspector = sa.inspect(bind)
        if fresh_inspector.has_table(table):
            existing = {index["name"] for index in fresh_inspector.get_indexes(table)}
            if name not in existing:
                op.create_index(name, table, columns, unique=False)

    if inspector.has_table("audit_log"):
        op.execute(
            "UPDATE audit_log SET actual_user_id = user_id "
            "WHERE actual_user_id IS NULL AND user_id IS NOT NULL"
        )
        op.execute(
            "UPDATE audit_log SET action_type = action "
            "WHERE action_type IS NULL AND action IS NOT NULL"
        )
        op.execute(
            "UPDATE audit_log SET acting_for_user_id = on_behalf_of_id "
            "WHERE acting_for_user_id IS NULL AND on_behalf_of_id IS NOT NULL"
        )
        op.execute(
            "UPDATE audit_log SET execution_context = "
            "CASE WHEN on_behalf_of_id IS NOT NULL THEN 'ACTING' ELSE 'SELF' END "
            "WHERE execution_context IS NULL OR TRIM(execution_context) = ''"
        )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table, names in (
        ("audit_log", (
            "ix_audit_formal_delegation",
            "ix_audit_execution_context_created",
            "ix_audit_acting_for_created",
            "ix_audit_actual_user_created",
        )),
        ("formal_delegations", (
            "ix_formal_delegations_delegator_window",
            "ix_formal_delegations_delegate_window",
        )),
        ("acting_permissions", (
            "ix_acting_permissions_principal_window",
            "ix_acting_permissions_actor_window",
        )),
    ):
        if inspector.has_table(table):
            existing = {index["name"] for index in sa.inspect(bind).get_indexes(table)}
            for name in names:
                if name in existing:
                    op.drop_index(name, table_name=table)

    if inspector.has_table("audit_log"):
        columns = _columns(bind, "audit_log")
        for name in (
            "user_agent",
            "ip_address",
            "object_id",
            "object_type",
            "module_name",
            "action_type",
            "execution_context",
            "formal_delegation_id",
            "acting_permission_id",
            "acting_for_user_id",
            "actual_user_id",
        ):
            if name in columns:
                with op.batch_alter_table("audit_log") as batch_op:
                    batch_op.drop_column(name)

    if inspector.has_table("formal_delegations"):
        op.drop_table("formal_delegations")
    if inspector.has_table("acting_permissions"):
        op.drop_table("acting_permissions")
