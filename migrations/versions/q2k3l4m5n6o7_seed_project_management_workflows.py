"""seed project management workflows

Revision ID: q2k3l4m5n6o7
Revises: p1j2k3l4m5n6
Create Date: 2026-08-26 21:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "q2k3l4m5n6o7"
down_revision = "p1j2k3l4m5n6"
branch_labels = None
depends_on = None


PROJECTS_NODE_CODE = "DIR_PROGRAMS"
ASSISTANT_NODE_CODE = "ASST_PROGRAMS"
SECRETARY_ROLE = "General_secretary"

WORKFLOWS = (
    ("PROJECT_NEW", "المشاريع الجديدة", "New Projects", "مسار المشاريع الجديدة", True),
    ("PROJECT_ACTIVE", "المشاريع القائمة", "Active Projects", "مسار المشاريع القائمة", False),
    ("PROJECT_REQUEST", "طلبات المشاريع", "Project Requests", "مسار طلبات المشاريع", True),
)


def _scalar(bind, sql, **params):
    return bind.execute(sa.text(sql), params).scalar()


def _node_id(bind, code):
    return _scalar(
        bind,
        "SELECT id FROM org_nodes WHERE code = :code AND is_active = 1 ORDER BY id LIMIT 1",
        code=code,
    )


def _request_type_id(bind, code):
    return _scalar(
        bind,
        "SELECT id FROM request_types WHERE code = :code ORDER BY id LIMIT 1",
        code=code,
    )


def _template_id(bind, name):
    return _scalar(
        bind,
        "SELECT id FROM workflow_templates WHERE name = :name ORDER BY id LIMIT 1",
        name=name,
    )


def _upsert_request_type(bind, code, name_ar, name_en):
    request_type_id = _request_type_id(bind, code)
    if request_type_id is None:
        bind.execute(
            sa.text(
                "INSERT INTO request_types "
                "(code, name_ar, name_en, is_active, created_at) "
                "VALUES (:code, :name_ar, :name_en, 1, CURRENT_TIMESTAMP)"
            ),
            {"code": code, "name_ar": name_ar, "name_en": name_en},
        )
        request_type_id = _request_type_id(bind, code)
    else:
        bind.execute(
            sa.text(
                "UPDATE request_types SET name_ar = :name_ar, name_en = :name_en, is_active = 1 "
                "WHERE id = :request_type_id"
            ),
            {
                "request_type_id": request_type_id,
                "name_ar": name_ar,
                "name_en": name_en,
            },
        )
    return request_type_id


def _upsert_template(bind, name, creator_id):
    template_id = _template_id(bind, name)
    if template_id is None:
        bind.execute(
            sa.text(
                "INSERT INTO workflow_templates "
                "(name, is_active, created_by_id, created_at, sla_days_default) "
                "VALUES (:name, 1, :creator_id, CURRENT_TIMESTAMP, 3)"
            ),
            {"name": name, "creator_id": creator_id},
        )
        template_id = _template_id(bind, name)
    else:
        bind.execute(
            sa.text(
                "UPDATE workflow_templates SET is_active = 1, sla_days_default = 3 "
                "WHERE id = :template_id"
            ),
            {"template_id": template_id},
        )
    return template_id


def _replace_steps(bind, template_id, projects_node_id, assistant_node_id, include_secretary):
    bind.execute(
        sa.text("DELETE FROM workflow_template_steps WHERE template_id = :template_id"),
        {"template_id": template_id},
    )
    targets = (
        (1, "ORG_NODE", projects_node_id, None),
        (2, "ORG_NODE", assistant_node_id, None),
    )
    if include_secretary:
        targets += ((3, "ROLE", None, SECRETARY_ROLE),)
    for step_order, kind, node_id, role in targets:
        bind.execute(
            sa.text(
                "INSERT INTO workflow_template_steps "
                "(template_id, step_order, mode, approver_kind, approver_org_node_id, approver_role, sla_days) "
                "VALUES (:template_id, :step_order, 'SEQUENTIAL', :kind, :node_id, :role, 3)"
            ),
            {
                "template_id": template_id,
                "step_order": step_order,
                "kind": kind,
                "node_id": node_id,
                "role": role,
            },
        )


def _upsert_rule(bind, request_type_id, template_id, projects_node_id):
    rule_id = _scalar(
        bind,
        "SELECT id FROM workflow_routing_rules "
        "WHERE request_type_id = :request_type_id AND template_id = :template_id "
        "AND org_node_id = :org_node_id ORDER BY id LIMIT 1",
        request_type_id=request_type_id,
        template_id=template_id,
        org_node_id=projects_node_id,
    )
    if rule_id is None:
        bind.execute(
            sa.text(
                "INSERT INTO workflow_routing_rules "
                "(request_type_id, organization_id, directorate_id, department_id, org_node_id, "
                "match_subtree, template_id, priority, is_active, created_at) "
                "VALUES (:request_type_id, NULL, NULL, NULL, :org_node_id, 1, "
                ":template_id, 1, 1, CURRENT_TIMESTAMP)"
            ),
            {
                "request_type_id": request_type_id,
                "org_node_id": projects_node_id,
                "template_id": template_id,
            },
        )
    else:
        bind.execute(
            sa.text(
                "UPDATE workflow_routing_rules SET match_subtree = 1, priority = 1, is_active = 1 "
                "WHERE id = :rule_id"
            ),
            {"rule_id": rule_id},
        )


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    required_tables = {
        "org_nodes",
        "users",
        "request_types",
        "workflow_templates",
        "workflow_template_steps",
        "workflow_routing_rules",
    }
    missing_tables = sorted(table for table in required_tables if not inspector.has_table(table))
    if missing_tables:
        raise RuntimeError("Workflow tables are unavailable: " + ", ".join(missing_tables))

    projects_node_id = _node_id(bind, PROJECTS_NODE_CODE)
    assistant_node_id = _node_id(bind, ASSISTANT_NODE_CODE)
    if projects_node_id is None or assistant_node_id is None:
        raise RuntimeError(
            "Approved project organization nodes are unavailable: "
            f"{PROJECTS_NODE_CODE}, {ASSISTANT_NODE_CODE}"
        )

    creator_id = _scalar(
        bind,
        "SELECT id FROM users WHERE upper(role) IN ('SUPER_ADMIN', 'ADMIN') ORDER BY id LIMIT 1",
    )
    for code, name_ar, name_en, template_name, include_secretary in WORKFLOWS:
        request_type_id = _upsert_request_type(bind, code, name_ar, name_en)
        template_id = _upsert_template(bind, template_name, creator_id)
        _replace_steps(
            bind,
            template_id,
            projects_node_id,
            assistant_node_id,
            include_secretary,
        )
        _upsert_rule(bind, request_type_id, template_id, projects_node_id)


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not all(
        inspector.has_table(table)
        for table in (
            "request_types",
            "workflow_templates",
            "workflow_template_steps",
            "workflow_routing_rules",
        )
    ):
        return

    has_requests = inspector.has_table("workflow_request")
    has_instances = inspector.has_table("workflow_instances")
    for code, _name_ar, _name_en, template_name, _include_secretary in reversed(WORKFLOWS):
        request_type_id = _request_type_id(bind, code)
        template_id = _template_id(bind, template_name)
        if request_type_id is not None and template_id is not None:
            bind.execute(
                sa.text(
                    "DELETE FROM workflow_routing_rules "
                    "WHERE request_type_id = :request_type_id AND template_id = :template_id"
                ),
                {"request_type_id": request_type_id, "template_id": template_id},
            )

        template_in_use = False
        if template_id is not None and has_instances:
            template_in_use = bool(_scalar(
                bind,
                "SELECT 1 FROM workflow_instances WHERE template_id = :template_id LIMIT 1",
                template_id=template_id,
            ))
        if template_id is not None and template_in_use:
            bind.execute(
                sa.text("UPDATE workflow_templates SET is_active = 0 WHERE id = :template_id"),
                {"template_id": template_id},
            )
        elif template_id is not None:
            bind.execute(
                sa.text("DELETE FROM workflow_template_steps WHERE template_id = :template_id"),
                {"template_id": template_id},
            )
            bind.execute(
                sa.text("DELETE FROM workflow_templates WHERE id = :template_id"),
                {"template_id": template_id},
            )

        request_type_in_use = False
        if request_type_id is not None and has_requests:
            request_type_in_use = bool(_scalar(
                bind,
                "SELECT 1 FROM workflow_request WHERE request_type_id = :request_type_id LIMIT 1",
                request_type_id=request_type_id,
            ))
        if request_type_id is not None and request_type_in_use:
            bind.execute(
                sa.text("UPDATE request_types SET is_active = 0 WHERE id = :request_type_id"),
                {"request_type_id": request_type_id},
            )
        elif request_type_id is not None:
            bind.execute(
                sa.text("DELETE FROM request_types WHERE id = :request_type_id"),
                {"request_type_id": request_type_id},
            )
