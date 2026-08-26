"""add request priority and normalize department-head cards

Revision ID: p1j2k3l4m5n6
Revises: o1j2k3l4m5n6
Create Date: 2026-08-26 19:00:00

The organization changes use stable employee emails and approved organization
codes so they are applied consistently outside the local SQLite database.
"""

from alembic import op
import sqlalchemy as sa


revision = "p1j2k3l4m5n6"
down_revision = "o1j2k3l4m5n6"
branch_labels = None
depends_on = None


MAJD_EMAIL = "majd.pncecs@gmail.com"
EMAD_EMAIL = "emad.pncecs@gmail.com"
SHOROUQ_EMAIL = "shorouq.pncecs@gmail.com"
SAWAFTA_EMAIL = "sawafta.pncecs@gmail.com"

FINANCE_NODE = "DEP_RES_FIN"
HR_NODE = "DEP_HR"
PROJECT_FINANCE_NODE = "DEP_PROJECT_FIN"

# Placements that existed before this approved correction. They are used only
# when an administrator explicitly downgrades this migration.
EMAD_OLD_NODE = "DEP_GENDER_AUDIT"
SHOROUQ_OLD_NODE = "OFFICE_SG"


def _scalar(bind, sql, **params):
    return bind.execute(sa.text(sql), params).scalar()


def _user_id(bind, email):
    return _scalar(
        bind,
        "SELECT id FROM users WHERE lower(email) = lower(:email) ORDER BY id LIMIT 1",
        email=email,
    )


def _node_id(bind, code):
    return _scalar(
        bind,
        "SELECT id FROM org_nodes WHERE code = :code AND is_active = 1 ORDER BY id LIMIT 1",
        code=code,
    )


def _set_manager(bind, node_id, manager_user_id, deputy_user_id=None):
    row_id = _scalar(
        bind,
        "SELECT id FROM org_node_managers WHERE node_id = :node_id",
        node_id=node_id,
    )
    if row_id is None:
        bind.execute(
            sa.text(
                "INSERT INTO org_node_managers "
                "(node_id, manager_user_id, deputy_user_id, updated_at, updated_by_id) "
                "VALUES (:node_id, :manager_user_id, :deputy_user_id, CURRENT_TIMESTAMP, NULL)"
            ),
            {
                "node_id": node_id,
                "manager_user_id": manager_user_id,
                "deputy_user_id": deputy_user_id,
            },
        )
        return
    bind.execute(
        sa.text(
            "UPDATE org_node_managers "
            "SET manager_user_id = :manager_user_id, deputy_user_id = :deputy_user_id, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = :row_id"
        ),
        {
            "manager_user_id": manager_user_id,
            "deputy_user_id": deputy_user_id,
            "row_id": row_id,
        },
    )


def _set_primary_assignment(bind, user_id, node_id):
    assignment_id = _scalar(
        bind,
        "SELECT id FROM org_node_assignments "
        "WHERE user_id = :user_id AND node_id = :node_id ORDER BY id LIMIT 1",
        user_id=user_id,
        node_id=node_id,
    )
    if assignment_id is None:
        assignment_id = _scalar(
            bind,
            "SELECT id FROM org_node_assignments "
            "WHERE user_id = :user_id AND is_primary = 1 ORDER BY id LIMIT 1",
            user_id=user_id,
        )
    if assignment_id is None:
        bind.execute(
            sa.text(
                "INSERT INTO org_node_assignments "
                "(user_id, node_id, title, is_primary, created_at, created_by_id) "
                "VALUES (:user_id, :node_id, NULL, 1, CURRENT_TIMESTAMP, NULL)"
            ),
            {"user_id": user_id, "node_id": node_id},
        )
        assignment_id = _scalar(
            bind,
            "SELECT id FROM org_node_assignments "
            "WHERE user_id = :user_id AND node_id = :node_id ORDER BY id LIMIT 1",
            user_id=user_id,
            node_id=node_id,
        )
    else:
        bind.execute(
            sa.text(
                "UPDATE org_node_assignments SET node_id = :node_id, title = NULL "
                "WHERE id = :assignment_id"
            ),
            {"node_id": node_id, "assignment_id": assignment_id},
        )

    bind.execute(
        sa.text(
            "UPDATE org_node_assignments SET is_primary = CASE WHEN id = :assignment_id THEN 1 ELSE 0 END "
            "WHERE user_id = :user_id"
        ),
        {"assignment_id": assignment_id, "user_id": user_id},
    )
    bind.execute(
        sa.text("UPDATE users SET org_node_id = :node_id WHERE id = :user_id"),
        {"node_id": node_id, "user_id": user_id},
    )


def _remove_from_node(bind, user_id, node_id):
    bind.execute(
        sa.text(
            "DELETE FROM org_node_assignments WHERE user_id = :user_id AND node_id = :node_id"
        ),
        {"user_id": user_id, "node_id": node_id},
    )
    bind.execute(
        sa.text(
            "UPDATE users SET org_node_id = NULL WHERE id = :user_id AND org_node_id = :node_id"
        ),
        {"user_id": user_id, "node_id": node_id},
    )


def _apply_org_changes(bind):
    inspector = sa.inspect(bind)
    required = {"users", "org_nodes", "org_node_assignments", "org_node_managers"}
    if any(not inspector.has_table(table) for table in required):
        return

    users = {
        "majd": _user_id(bind, MAJD_EMAIL),
        "emad": _user_id(bind, EMAD_EMAIL),
        "shorouq": _user_id(bind, SHOROUQ_EMAIL),
        "sawafta": _user_id(bind, SAWAFTA_EMAIL),
    }
    if not any(users.values()):
        return

    nodes = {
        "finance": _node_id(bind, FINANCE_NODE),
        "hr": _node_id(bind, HR_NODE),
        "project_finance": _node_id(bind, PROJECT_FINANCE_NODE),
    }
    missing = [name for name, node_id in nodes.items() if node_id is None]
    if missing:
        raise RuntimeError("Approved organization nodes were not found: " + ", ".join(missing))

    if users["majd"] is not None:
        _set_primary_assignment(bind, users["majd"], nodes["finance"])
        _set_manager(bind, nodes["finance"], users["majd"])
    if users["sawafta"] is not None:
        _remove_from_node(bind, users["sawafta"], nodes["finance"])
    if users["emad"] is not None:
        _set_primary_assignment(bind, users["emad"], nodes["hr"])
        _set_manager(bind, nodes["hr"], users["emad"])
    if users["shorouq"] is not None:
        _set_primary_assignment(bind, users["shorouq"], nodes["project_finance"])
        _set_manager(bind, nodes["project_finance"], users["shorouq"])


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("workflow_request"):
        columns = {column["name"] for column in inspector.get_columns("workflow_request")}
        if "priority" not in columns:
            with op.batch_alter_table("workflow_request") as batch_op:
                batch_op.add_column(
                    sa.Column(
                        "priority",
                        sa.String(length=20),
                        nullable=False,
                        server_default="NORMAL",
                    )
                )
        indexes = {index["name"] for index in sa.inspect(bind).get_indexes("workflow_request")}
        if "ix_workflow_request_priority" not in indexes:
            op.create_index(
                "ix_workflow_request_priority",
                "workflow_request",
                ["priority"],
            )

    _apply_org_changes(bind)


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    required = {"users", "org_nodes", "org_node_assignments", "org_node_managers"}
    if all(inspector.has_table(table) for table in required):
        majd_id = _user_id(bind, MAJD_EMAIL)
        emad_id = _user_id(bind, EMAD_EMAIL)
        shorouq_id = _user_id(bind, SHOROUQ_EMAIL)
        sawafta_id = _user_id(bind, SAWAFTA_EMAIL)
        finance_id = _node_id(bind, FINANCE_NODE)
        hr_id = _node_id(bind, HR_NODE)
        project_finance_id = _node_id(bind, PROJECT_FINANCE_NODE)
        emad_old_id = _node_id(bind, EMAD_OLD_NODE)
        shorouq_old_id = _node_id(bind, SHOROUQ_OLD_NODE)

        if finance_id is not None:
            _set_manager(bind, finance_id, None, majd_id)
            if majd_id is not None:
                _remove_from_node(bind, majd_id, finance_id)
            if sawafta_id is not None:
                _set_primary_assignment(bind, sawafta_id, finance_id)
        if hr_id is not None:
            _set_manager(bind, hr_id, emad_id)
        if emad_id is not None and emad_old_id is not None:
            _set_primary_assignment(bind, emad_id, emad_old_id)
        if project_finance_id is not None:
            _set_manager(bind, project_finance_id, shorouq_id)
        if shorouq_id is not None and shorouq_old_id is not None:
            _set_primary_assignment(bind, shorouq_id, shorouq_old_id)

    if inspector.has_table("workflow_request"):
        indexes = {index["name"] for index in sa.inspect(bind).get_indexes("workflow_request")}
        if "ix_workflow_request_priority" in indexes:
            op.drop_index("ix_workflow_request_priority", table_name="workflow_request")
        columns = {column["name"] for column in sa.inspect(bind).get_columns("workflow_request")}
        if "priority" in columns:
            with op.batch_alter_table("workflow_request") as batch_op:
                batch_op.drop_column("priority")
