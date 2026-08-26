"""Approved predefined workflows for the General Directorate of Projects."""

from __future__ import annotations

from dataclasses import dataclass

from extensions import db
from models import (
    OrgNode,
    RequestType,
    User,
    WorkflowRoutingRule,
    WorkflowTemplate,
    WorkflowTemplateStep,
)


PROJECTS_DIRECTORATE_NODE_CODE = "DIR_PROGRAMS"
PROJECTS_ASSISTANT_NODE_CODE = "ASST_PROGRAMS"
SECRETARY_GENERAL_ROLE = "General_secretary"


@dataclass(frozen=True)
class ProjectWorkflowDefinition:
    request_type_code: str
    request_type_name_ar: str
    request_type_name_en: str
    template_name: str
    description: str
    include_secretary_general: bool


PROJECT_WORKFLOW_DEFINITIONS = (
    ProjectWorkflowDefinition(
        request_type_code="PROJECT_NEW",
        request_type_name_ar="المشاريع الجديدة",
        request_type_name_en="New Projects",
        template_name="مسار المشاريع الجديدة",
        description="من التسجيل حتى الاعتماد.",
        include_secretary_general=True,
    ),
    ProjectWorkflowDefinition(
        request_type_code="PROJECT_ACTIVE",
        request_type_name_ar="المشاريع القائمة",
        request_type_name_en="Active Projects",
        template_name="مسار المشاريع القائمة",
        description="للمتابعة والتقارير والإنجاز والمشاكل.",
        include_secretary_general=False,
    ),
    ProjectWorkflowDefinition(
        request_type_code="PROJECT_REQUEST",
        request_type_name_ar="طلبات المشاريع",
        request_type_name_en="Project Requests",
        template_name="مسار طلبات المشاريع",
        description="للتعديلات والتمديدات والدفعات والإلغاء وغيرها.",
        include_secretary_general=True,
    ),
)

PROJECT_WORKFLOW_METADATA_BY_TEMPLATE_NAME = {
    definition.template_name: {
        "description": definition.description,
        "request_type_name": definition.request_type_name_ar,
    }
    for definition in PROJECT_WORKFLOW_DEFINITIONS
}


class ProjectWorkflowConfigurationError(RuntimeError):
    """Raised when the approved organization nodes are unavailable."""


def _active_node(node_code: str) -> OrgNode | None:
    return (
        OrgNode.query
        .filter_by(code=node_code, is_active=True)
        .order_by(OrgNode.id.asc())
        .first()
    )


def _creator_id() -> int | None:
    creator = (
        User.query
        .filter(db.func.upper(User.role).in_(("SUPER_ADMIN", "ADMIN")))
        .order_by(User.id.asc())
        .first()
    )
    return int(creator.id) if creator else None


def _upsert_request_type(
    definition: ProjectWorkflowDefinition,
    *,
    preserve_existing: bool = False,
) -> RequestType:
    request_type = RequestType.query.filter_by(code=definition.request_type_code).first()
    if request_type is None:
        request_type = RequestType(code=definition.request_type_code)
        db.session.add(request_type)
    elif preserve_existing:
        return request_type
    request_type.name_ar = definition.request_type_name_ar
    request_type.name_en = definition.request_type_name_en
    request_type.is_active = True
    db.session.flush()
    return request_type


def _upsert_template(
    definition: ProjectWorkflowDefinition,
    *,
    projects_node_id: int,
    assistant_node_id: int,
    creator_id: int | None,
    preserve_existing: bool = False,
) -> WorkflowTemplate:
    template = (
        WorkflowTemplate.query
        .filter_by(name=definition.template_name)
        .order_by(WorkflowTemplate.id.asc())
        .first()
    )
    if template is None:
        template = WorkflowTemplate(name=definition.template_name, created_by_id=creator_id)
        db.session.add(template)
        db.session.flush()
    elif preserve_existing:
        return template

    template.is_active = True
    template.sla_days_default = 3

    WorkflowTemplateStep.query.filter_by(template_id=template.id).delete(
        synchronize_session=False
    )
    step_targets: list[tuple[str, int | str]] = [
        ("ORG_NODE", projects_node_id),
        ("ORG_NODE", assistant_node_id),
    ]
    if definition.include_secretary_general:
        step_targets.append(("ROLE", SECRETARY_GENERAL_ROLE))

    for step_order, (kind, target) in enumerate(step_targets, start=1):
        db.session.add(WorkflowTemplateStep(
            template_id=template.id,
            step_order=step_order,
            mode="SEQUENTIAL",
            approver_kind=kind,
            approver_org_node_id=int(target) if kind == "ORG_NODE" else None,
            approver_role=str(target) if kind == "ROLE" else None,
            sla_days=3,
        ))

    db.session.flush()
    return template


def _upsert_routing_rule(
    request_type: RequestType,
    template: WorkflowTemplate,
    projects_node_id: int,
    *,
    preserve_existing: bool = False,
) -> WorkflowRoutingRule:
    rule_query = WorkflowRoutingRule.query.filter_by(
        request_type_id=request_type.id,
        template_id=template.id,
    )
    if not preserve_existing:
        rule_query = rule_query.filter_by(org_node_id=projects_node_id)
    rule = rule_query.order_by(WorkflowRoutingRule.id.asc()).first()
    if rule is not None and preserve_existing:
        return rule
    if rule is None:
        rule = WorkflowRoutingRule(
            request_type_id=request_type.id,
            template_id=template.id,
            org_node_id=projects_node_id,
        )
        db.session.add(rule)
    rule.organization_id = None
    rule.directorate_id = None
    rule.department_id = None
    rule.match_subtree = True
    rule.priority = 1
    rule.is_active = True
    db.session.flush()
    return rule


def upsert_project_workflows(*, preserve_existing: bool = False) -> list[dict]:
    """Create or refresh the three approved project workflow definitions.

    The caller owns the transaction so the helper can be used by a script,
    tests, or another controlled deployment task.  ``preserve_existing`` is
    intended for application startup: it creates missing records while leaving
    administrator edits to existing templates, steps, request types, and
    routing rules untouched.
    """

    projects_node = _active_node(PROJECTS_DIRECTORATE_NODE_CODE)
    assistant_node = _active_node(PROJECTS_ASSISTANT_NODE_CODE)
    missing = []
    if projects_node is None:
        missing.append(PROJECTS_DIRECTORATE_NODE_CODE)
    if assistant_node is None:
        missing.append(PROJECTS_ASSISTANT_NODE_CODE)
    if missing:
        raise ProjectWorkflowConfigurationError(
            "تعذر إنشاء مسارات المشاريع لعدم وجود عناصر الهيكلية: "
            + "، ".join(missing)
        )

    creator_id = _creator_id()
    results = []
    for definition in PROJECT_WORKFLOW_DEFINITIONS:
        request_type = _upsert_request_type(
            definition,
            preserve_existing=preserve_existing,
        )
        template = _upsert_template(
            definition,
            projects_node_id=int(projects_node.id),
            assistant_node_id=int(assistant_node.id),
            creator_id=creator_id,
            preserve_existing=preserve_existing,
        )
        rule = _upsert_routing_rule(
            request_type,
            template,
            int(projects_node.id),
            preserve_existing=preserve_existing,
        )
        results.append({
            "request_type_id": int(request_type.id),
            "template_id": int(template.id),
            "routing_rule_id": int(rule.id),
            "template_name": template.name,
            "description": definition.description,
            "step_count": WorkflowTemplateStep.query.filter_by(
                template_id=template.id
            ).count(),
        })
    return results
