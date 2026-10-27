"""Start-permission rules for predefined workflow templates.

These rules intentionally control only who may create a new request from a
template.  They do not participate in request visibility or in permissions
for a request after it has started.
"""

from __future__ import annotations

from collections.abc import Iterable

from extensions import db
from models import (
    Department,
    Directorate,
    Division,
    EmployeeFile,
    OrgNode,
    OrgNodeAssignment,
    OrgUnitAssignment,
    Organization,
    Section,
    Team,
    Unit,
    User,
    WorkflowTemplateStartAccess,
)


START_ACCESS_NONE = "NONE"
START_ACCESS_ALL = "ALL"
START_ACCESS_SELECTED = "SELECTED"
START_ACCESS_MODES = frozenset({
    START_ACCESS_NONE,
    START_ACCESS_ALL,
    START_ACCESS_SELECTED,
})

START_ACCESS_AUDIENCE_TYPES = (
    # ORGANIZATION is retained for compatibility with older/custom data;
    # the editor exposes the three requested hierarchy levels below it.
    "ORGANIZATION",
    "DIRECTORATE",
    "DEPARTMENT",
    "SECTION",
    "USER",
)

START_ACCESS_MODE_LABELS = {
    START_ACCESS_NONE: "لا أحد",
    START_ACCESS_ALL: "الجميع",
    START_ACCESS_SELECTED: "تحديد جهات وأشخاص",
}

START_ACCESS_AUDIENCE_LABELS = {
    "ORGANIZATION": "مؤسسة",
    "DIRECTORATE": "إدارة عامة",
    "DEPARTMENT": "دائرة",
    "SECTION": "قسم",
    "USER": "شخص محدد",
}

_LEGACY_ORG_MODELS = {
    "ORGANIZATION": Organization,
    "DIRECTORATE": Directorate,
    "UNIT": Unit,
    "DEPARTMENT": Department,
    "SECTION": Section,
    "DIVISION": Division,
    "TEAM": Team,
}

_PARENT_FIELDS = {
    "ORGANIZATION": (),
    "DIRECTORATE": (("ORGANIZATION", "organization_id"),),
    "UNIT": (("ORGANIZATION", "organization_id"),),
    "DEPARTMENT": (
        ("DIRECTORATE", "directorate_id"),
        ("UNIT", "unit_id"),
    ),
    "SECTION": (
        ("DEPARTMENT", "department_id"),
        ("UNIT", "unit_id"),
        ("DIRECTORATE", "directorate_id"),
    ),
    "DIVISION": (
        ("SECTION", "section_id"),
        ("DEPARTMENT", "department_id"),
    ),
    "TEAM": (
        ("DIVISION", "division_id"),
        ("SECTION", "section_id"),
    ),
}


def normalize_start_access_mode(value: str | None) -> str:
    mode = (value or "").strip().upper()
    if mode in START_ACCESS_MODES:
        return mode
    return START_ACCESS_NONE


def start_access_mode_label(value: str | None) -> str:
    return START_ACCESS_MODE_LABELS.get(
        normalize_start_access_mode(value),
        START_ACCESS_MODE_LABELS[START_ACCESS_NONE],
    )


def start_access_rule_ids(template) -> dict[str, set[int]]:
    """Return selected audience IDs grouped by their audience type."""
    grouped = {audience_type: set() for audience_type in START_ACCESS_AUDIENCE_TYPES}
    if not template:
        return grouped

    rules = getattr(template, "start_access_rules", None)
    if rules is None and getattr(template, "id", None):
        rules = WorkflowTemplateStartAccess.query.filter_by(
            template_id=int(template.id)
        ).all()

    for rule in rules or ():
        audience_type = (getattr(rule, "audience_type", None) or "").strip().upper()
        audience_id = getattr(rule, "audience_id", None)
        if audience_type not in grouped or audience_id is None:
            continue
        try:
            grouped[audience_type].add(int(audience_id))
        except (TypeError, ValueError):
            continue
    return grouped


def _add_legacy_unit_scope(
    scope: set[tuple[str, int]],
    unit_type: str | None,
    unit_id,
    seen: set[tuple[str, int]] | None = None,
) -> None:
    unit_type = (unit_type or "").strip().upper()
    try:
        unit_id = int(unit_id)
    except (TypeError, ValueError):
        return
    if unit_type not in _LEGACY_ORG_MODELS or unit_id <= 0:
        return

    seen = seen if seen is not None else set()
    key = (unit_type, unit_id)
    if key in seen:
        return
    seen.add(key)
    scope.add(key)

    source = db.session.get(_LEGACY_ORG_MODELS[unit_type], unit_id)
    if not source:
        return
    for parent_type, field_name in _PARENT_FIELDS.get(unit_type, ()):
        parent_id = getattr(source, field_name, None)
        if parent_id:
            _add_legacy_unit_scope(scope, parent_type, parent_id, seen)


def _add_dynamic_node_scope(
    scope: set[tuple[str, int]],
    node_id,
    seen: set[int] | None = None,
) -> None:
    """Add a unified-org placement and its legacy-compatible ancestors."""
    try:
        node_id = int(node_id)
    except (TypeError, ValueError):
        return
    if node_id <= 0:
        return

    seen = seen if seen is not None else set()
    if node_id in seen:
        return
    seen.add(node_id)

    node = db.session.get(OrgNode, node_id)
    if not node:
        return

    # Nodes synced from the legacy hierarchy carry the exact fixed-table
    # identity, allowing an access rule selected in the editor to match both
    # old HR placement rows and unified-org assignments.
    legacy_type = (getattr(node, "legacy_type", None) or "").strip().upper()
    legacy_id = getattr(node, "legacy_id", None)
    if legacy_type and legacy_id:
        _add_legacy_unit_scope(scope, legacy_type, legacy_id)

    if getattr(node, "parent_id", None):
        _add_dynamic_node_scope(scope, node.parent_id, seen)


def user_organization_scope(user) -> set[tuple[str, int]]:
    """Return the fixed-hierarchy units occupied by a user and their parents.

    Placement data exists in more than one generation of the application.  We
    deliberately read the employee file, the legacy User columns, and HR org
    assignments so the start rule remains correct during gradual data cleanup.
    """
    if not user:
        return set()
    if not getattr(user, "id", None):
        try:
            user = db.session.get(User, int(user))
        except (TypeError, ValueError):
            return set()
    if not user:
        return set()

    scope: set[tuple[str, int]] = set()
    seen: set[tuple[str, int]] = set()

    employee_file = db.session.get(EmployeeFile, int(user.id))
    for source in (user, employee_file):
        if not source:
            continue
        for unit_type, field_name in (
            ("ORGANIZATION", "organization_id"),
            ("DIRECTORATE", "directorate_id"),
            ("DEPARTMENT", "department_id"),
            ("UNIT", "unit_id"),
            ("SECTION", "section_id"),
            ("DIVISION", "division_id"),
        ):
            _add_legacy_unit_scope(
                scope,
                unit_type,
                getattr(source, field_name, None),
                seen,
            )

    assignments = (
        OrgUnitAssignment.query
        .filter_by(user_id=int(user.id))
        .order_by(OrgUnitAssignment.is_primary.desc(), OrgUnitAssignment.id.asc())
        .all()
    )
    for assignment in assignments:
        _add_legacy_unit_scope(
            scope,
            getattr(assignment, "unit_type", None),
            getattr(assignment, "unit_id", None),
            seen,
        )

    dynamic_seen: set[int] = set()
    if getattr(user, "org_node_id", None):
        _add_dynamic_node_scope(scope, user.org_node_id, dynamic_seen)
    for assignment in (
        OrgNodeAssignment.query
        .filter_by(user_id=int(user.id))
        .order_by(OrgNodeAssignment.is_primary.desc(), OrgNodeAssignment.id.asc())
        .all()
    ):
        _add_dynamic_node_scope(scope, assignment.node_id, dynamic_seen)

    return scope


def can_user_start_template(user, template) -> bool:
    """Return whether ``user`` may start a new request from ``template``."""
    if not user or not template:
        return False

    mode = normalize_start_access_mode(getattr(template, "start_access_mode", None))
    if mode == START_ACCESS_ALL:
        return True
    if mode == START_ACCESS_NONE:
        return False

    try:
        user_id = int(user.id)
    except (AttributeError, TypeError, ValueError):
        return False

    scope = user_organization_scope(user)
    for audience_type, audience_ids in start_access_rule_ids(template).items():
        if audience_type == "USER" and user_id in audience_ids:
            return True
        if any((audience_type, int(audience_id)) in scope for audience_id in audience_ids):
            return True
    return False


def filter_startable_templates(user, templates: Iterable) -> list:
    return [
        template
        for template in (templates or ())
        if can_user_start_template(user, template)
    ]


def start_access_summary(template) -> str:
    """Build a short label suitable for admin list/detail screens."""
    mode = normalize_start_access_mode(getattr(template, "start_access_mode", None))
    if mode != START_ACCESS_SELECTED:
        return start_access_mode_label(mode)

    counts = [
        f"{START_ACCESS_AUDIENCE_LABELS[audience_type]}: {len(ids)}"
        for audience_type, ids in start_access_rule_ids(template).items()
        if ids
    ]
    return "، ".join(counts) if counts else START_ACCESS_MODE_LABELS[START_ACCESS_NONE]
