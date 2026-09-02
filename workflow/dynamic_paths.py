from __future__ import annotations

from datetime import date

from extensions import db
from models import (
    Committee,
    CommitteeAssignee,
    Department,
    Directorate,
    Division,
    EmployeeFile,
    EmployeeSecondment,
    OrgNode,
    OrgNodeAssignment,
    OrgNodeManager,
    OrgNodeType,
    OrgUnitAssignment,
    OrgUnitManager,
    Organization,
    Section,
    Team,
    TeamMembership,
    Unit,
    User,
)
from utils.approved_org_structure import find_approved_org_node_by_name
from utils.org_dynamic import resolve_user_org_node_id


MAX_DYNAMIC_TARGETS = 20
FINAL_SECRETARY_GENERAL_REF = "FINAL_SECRETARY_GENERAL"
DYNAMIC_DIRECT_DELIVERY_REF = "DIRECT_DELIVERY"
DYNAMIC_DELIVERY_MODE_HIERARCHICAL = "HIERARCHICAL"
DYNAMIC_DELIVERY_MODE_DIRECT = "DIRECT"
DYNAMIC_RETURN_REASON = "عودة المسار وفق التسلسل الإداري"
COMMITTEE_DELIVERY_MODES = {
    "ALL": ("Committee_ALL", "كل أعضاء اللجنة"),
    "CHAIR": ("Committee_CHAIR", "رئيس اللجنة"),
    "SECRETARY": ("Committee_SECRETARY", "مقرر اللجنة"),
}


def _node_type_code(node: OrgNode | None) -> str:
    node_type = getattr(node, "type", None)
    return (getattr(node_type, "code", "") or "").strip().upper()


def _node_type_name(node: OrgNode | None) -> str:
    node_type = getattr(node, "type", None)
    return (getattr(node_type, "name_ar", "") or getattr(node_type, "name_en", "") or "").strip()


def _is_secretary_general_node(node: OrgNode | None) -> bool:
    """The secretary general is opt-in and must never be an implicit route step."""
    if _node_type_code(node) == "SECRETARY_GENERAL":
        return True
    normalized_name = _normalized_org_text(getattr(node, "name_ar", None))
    normalized_english = _normalized_org_text(getattr(node, "name_en", None))
    return normalized_name in {"الامينالعام", "امينعامالمجلس"} or normalized_english == "secretarygeneral"


def can_use_direct_dynamic_delivery(user: User | None) -> bool:
    """Whether the actor may send a dynamic route directly to an employee.

    Direct delivery is deliberately limited to the General Secretary role.  The
    role check accepts the legacy underscore spelling as well as the canonical
    ``GENERAL-SECRETARY`` code used by the permissions seed.
    """
    if not user:
        return False
    try:
        if any(
            user.has_role(role)
            for role in ("GENERAL-SECRETARY", "GENERAL_SECRETARY")
        ):
            return True
    except Exception:
        pass

    normalized_role = _normalized_org_text(getattr(user, "role", ""))
    return normalized_role in {
        "generalsecretary",
        "الامينالعام",
        "امينعامالمجلس",
    }


def _normalize_dynamic_delivery_mode(value: str | None) -> str | None:
    normalized = (value or DYNAMIC_DELIVERY_MODE_HIERARCHICAL).strip().upper()
    aliases = {
        "HIERARCHY": DYNAMIC_DELIVERY_MODE_HIERARCHICAL,
        "STRUCTURAL": DYNAMIC_DELIVERY_MODE_HIERARCHICAL,
        "DIRECT": DYNAMIC_DELIVERY_MODE_DIRECT,
    }
    return aliases.get(normalized, normalized if normalized == DYNAMIC_DELIVERY_MODE_HIERARCHICAL else None)


def _normalized_org_text(value: str | None) -> str:
    text = (value or "").strip().lower()
    for source, replacement in (
        ("أ", "ا"), ("إ", "ا"), ("آ", "ا"),
        ("ى", "ي"), ("ة", "ه"), ("ؤ", "و"), ("ئ", "ي"),
    ):
        text = text.replace(source, replacement)
    return "".join(character for character in text if character.isalnum())


def node_chain(node_id: int | None) -> list[OrgNode]:
    """Return the real organizational chain from root to the selected node."""
    if not node_id:
        return []

    chain: list[OrgNode] = []
    seen: set[int] = set()
    current_id = int(node_id)
    while current_id and current_id not in seen and len(chain) < 200:
        seen.add(current_id)
        node = db.session.get(OrgNode, current_id)
        if not node:
            return []
        chain.append(node)
        current_id = int(node.parent_id) if node.parent_id else 0
    chain.reverse()
    return chain


def node_path_label(node_or_id: OrgNode | int | None) -> str:
    node_id = getattr(node_or_id, "id", node_or_id)
    chain = node_chain(int(node_id)) if node_id else []
    return " ← ".join(
        f"{_node_type_name(node) or _node_type_code(node)}: {node.name_ar}"
        for node in chain
    )


def hierarchy_position_label(
    user: User | None,
    *,
    routing_node_label: str | None = None,
    org_node_id: int | None = None,
) -> str:
    """Return the user's managerial role for the node represented by a workflow step."""
    user_id = int(getattr(user, "id", 0) or 0)
    if not user_id:
        return ""

    assignments = (
        OrgNodeManager.query
        .filter(
            (OrgNodeManager.manager_user_id == user_id)
            | (OrgNodeManager.deputy_user_id == user_id)
        )
        .order_by(OrgNodeManager.node_id.asc())
        .all()
    )
    if not assignments:
        return ""

    selected_assignment = None
    if org_node_id:
        selected_assignment = next(
            (
                assignment
                for assignment in assignments
                if int(assignment.node_id) == int(org_node_id)
            ),
            None,
        )

    if selected_assignment is None and routing_node_label:
        step_node_label = routing_node_label.rsplit("←", 1)[-1].strip()
        selected_assignment = next(
            (
                assignment
                for assignment in assignments
                if assignment.node
                and (
                    f"{_node_type_name(assignment.node) or _node_type_code(assignment.node)}: "
                    f"{assignment.node.name_ar}"
                ).strip() == step_node_label
            ),
            None,
        )

    if selected_assignment is None:
        primary_node_id = resolve_user_org_node_id(user)
        selected_assignment = next(
            (
                assignment
                for assignment in assignments
                if primary_node_id and int(assignment.node_id) == int(primary_node_id)
            ),
            None,
        )

    if selected_assignment is None and len(assignments) == 1:
        selected_assignment = assignments[0]
    if selected_assignment is None or not selected_assignment.node:
        return ""

    node = selected_assignment.node
    configured_position = (
        OrgNodeAssignment.query
        .filter_by(user_id=user_id, node_id=int(node.id))
        .filter(OrgNodeAssignment.title.isnot(None))
        .order_by(OrgNodeAssignment.is_primary.desc(), OrgNodeAssignment.id.asc())
        .first()
    )
    if configured_position and (configured_position.title or "").strip():
        return configured_position.title.strip()

    node_type_name = _node_type_name(node) or _node_type_code(node)
    normalized_node_name = _normalized_org_text(node.name_ar)
    assistant_secretary_label = _normalized_org_text("مساعد الأمين العام")
    general_administration_label = _normalized_org_text("الإدارة العامة")
    if int(selected_assignment.manager_user_id or 0) == user_id:
        if _node_type_code(node) == "SEC_GEN_ASSIST" or assistant_secretary_label in normalized_node_name:
            return node.name_ar
        if general_administration_label in normalized_node_name:
            return f"مدير عام {node.name_ar}"
        role_name = "مدير"
    elif int(selected_assignment.deputy_user_id or 0) == user_id:
        if _node_type_code(node) == "SEC_GEN_ASSIST" or assistant_secretary_label in normalized_node_name:
            return f"نائب {node.name_ar}"
        if general_administration_label in normalized_node_name:
            return f"نائب المدير العام {node.name_ar}"
        role_name = "نائب مدير"
    else:
        return ""
    return f"{role_name} {node_type_name}: {node.name_ar}"


def administration_anchor_id(chain: list[OrgNode]) -> int | None:
    """Resolve the nearest general-administration branch for direct selection."""
    if not chain:
        return None

    for node in reversed(chain):
        code = _node_type_code(node)
        node_text = _normalized_org_text(
            " ".join(filter(None, (
                getattr(node, "name_ar", None),
                getattr(node, "name_en", None),
                _node_type_name(node),
            )))
        )
        if code in {"GENERAL_ADMINISTRATION", "GENERAL_DIRECTORATE"}:
            return int(node.id)
        if "الادارهالعامه" in node_text or "generaladministration" in node_text:
            return int(node.id)

    for node in reversed(chain):
        code = _node_type_code(node)
        type_name = _normalized_org_text(_node_type_name(node))
        if code in {"DIRECTORATE", "ADMINISTRATION"}:
            return int(node.id)
        if "اداره" in type_name:
            return int(node.id)

    root_index = 1 if len(chain) > 1 and _node_type_code(chain[0]) == "ORGANIZATION" else 0
    return int(chain[root_index].id)


def same_administration(source_chain: list[OrgNode], target_chain: list[OrgNode]) -> bool:
    source_ids = {int(node.id) for node in source_chain}
    target_ids = {int(node.id) for node in target_chain}
    if source_chain and target_chain:
        source_leaf_id = int(source_chain[-1].id)
        target_leaf_id = int(target_chain[-1].id)
        if source_leaf_id in target_ids or target_leaf_id in source_ids:
            return True

    source_anchor = administration_anchor_id(source_chain)
    target_anchor = administration_anchor_id(target_chain)
    return bool(source_anchor and target_anchor and source_anchor == target_anchor)


def structural_route_nodes(source_node_id: int, target_node_id: int) -> list[OrgNode]:
    """Return nodes traversed upward to the common parent and down to the target."""
    source_chain = node_chain(source_node_id)
    target_chain = node_chain(target_node_id)
    if not source_chain or not target_chain:
        return []

    common_length = 0
    for source_node, target_node in zip(source_chain, target_chain):
        if int(source_node.id) != int(target_node.id):
            break
        common_length += 1

    upward = list(reversed(source_chain[common_length:]))
    common = [source_chain[common_length - 1]] if common_length else []
    downward = target_chain[common_length:]

    route: list[OrgNode] = []
    seen: set[int] = set()
    for node in upward + common + downward:
        node_id = int(node.id)
        if node_id in seen:
            continue
        seen.add(node_id)
        route.append(node)
    return route


def vertical_structural_route_nodes(
    source_chain: list[OrgNode],
    target_chain: list[OrgNode],
) -> list[OrgNode]:
    """Return the automatic vertical route from source to destination.

    Dynamic routes must always climb from the requester's placement and then
    descend to the destination.  Top governance levels are never inserted
    implicitly; the secretary general remains available through its dedicated
    final-step option.
    """
    excluded_type_codes = {"ORGANIZATION", "CHAIRPERSON", "SECRETARY_GENERAL"}
    return [
        node
        for node in structural_route_nodes(
            int(source_chain[-1].id),
            int(target_chain[-1].id),
        )
        if _node_type_code(node) not in excluded_type_codes
        and not _is_secretary_general_node(node)
    ]


def route_origin(user: User, chain: list[OrgNode] | None = None) -> dict:
    """Describe the requester as the non-approval starting point of a route."""
    effective_chain = chain if chain is not None else node_chain(resolve_user_org_node_id(user))
    return {
        "user_id": int(user.id),
        "label": user.full_name or user.email or f"مستخدم #{user.id}",
        "job_title": (getattr(user, "job_title", None) or "").strip(),
        "node_label": node_path_label(effective_chain[-1]) if effective_chain else "",
    }


def _node_allows_approval(node: OrgNode) -> bool:
    node_type = getattr(node, "type", None)
    return bool(
        getattr(node, "is_active", False)
        and node_type
        and getattr(node_type, "is_active", False)
        and getattr(node_type, "allow_in_approvals", False)
    )


def _manager_for_node(node: OrgNode) -> tuple[User | None, str | None]:
    manager = OrgNodeManager.query.filter_by(node_id=int(node.id)).first()
    if not manager:
        return None, None
    user_id = manager.manager_user_id or manager.deputy_user_id
    if not user_id:
        return None, None
    user = db.session.get(User, int(user_id))
    role = "مسؤول" if manager.manager_user_id else "نائب المسؤول"
    return user, role


def _manager_candidates_for_node(node: OrgNode) -> list[tuple[User, str]]:
    """Return every configured manager/deputy for a node in display order."""
    manager = OrgNodeManager.query.filter_by(node_id=int(node.id)).first()
    if not manager:
        return []

    candidates: list[tuple[User, str]] = []
    seen_user_ids: set[int] = set()
    for user_id, role in (
        (manager.manager_user_id, "المسؤول"),
        (manager.deputy_user_id, "نائب المسؤول"),
    ):
        if not user_id or int(user_id) in seen_user_ids:
            continue
        user = db.session.get(User, int(user_id))
        if user:
            seen_user_ids.add(int(user_id))
            candidates.append((user, role))
    return candidates


def _step_node(step: dict) -> OrgNode | None:
    node_id = step.get("node_id") or step.get("approver_org_node_id")
    try:
        return db.session.get(OrgNode, int(node_id)) if node_id else None
    except (TypeError, ValueError):
        return None


def _step_user_id(step: dict) -> int | None:
    user_id = step.get("approver_user_id")
    if user_id:
        return int(user_id)
    node = _step_node(step)
    user, _role = _manager_for_node(node) if node else (None, None)
    return int(user.id) if user else None


def _user_manages_node(user_id: int | None, node: OrgNode | None) -> bool:
    if not user_id or not node:
        return False
    for current_node in node_chain(node.id):
        manager = OrgNodeManager.query.filter_by(node_id=int(current_node.id)).first()
        if not manager:
            continue
        if int(manager.manager_user_id or 0) == int(user_id):
            return True
        if int(manager.deputy_user_id or 0) == int(user_id):
            return True
    return False


def _user_is_direct_manager_of(manager_id: int | None, user_id: int | None) -> bool:
    if not manager_id or not user_id or int(manager_id) == int(user_id):
        return False
    employee_file = db.session.get(EmployeeFile, int(user_id))
    if employee_file and int(employee_file.direct_manager_user_id or 0) == int(manager_id):
        return True

    today = date.today().isoformat()
    return EmployeeSecondment.query.filter(
        EmployeeSecondment.user_id == int(user_id),
        EmployeeSecondment.direct_manager_user_id == int(manager_id),
        (EmployeeSecondment.date_from.is_(None) | (EmployeeSecondment.date_from <= today)),
        (EmployeeSecondment.date_to.is_(None) | (EmployeeSecondment.date_to >= today)),
    ).first() is not None


def _steps_are_non_managerial_peers(first_step: dict, second_step: dict) -> bool:
    first_node = _step_node(first_step)
    second_node = _step_node(second_step)
    if not first_node or not second_node:
        return False

    first_chain = node_chain(first_node.id)
    second_chain = node_chain(second_node.id)
    if not first_chain or not second_chain:
        return False
    if len(first_chain) != len(second_chain):
        return False
    if _node_type_code(first_node) != _node_type_code(second_node):
        return False

    first_user_id = _step_user_id(first_step)
    second_user_id = _step_user_id(second_step)
    if _user_is_direct_manager_of(first_user_id, second_user_id):
        return False
    if _user_is_direct_manager_of(second_user_id, first_user_id):
        return False
    if _user_manages_node(first_user_id, second_node):
        return False
    if _user_manages_node(second_user_id, first_node):
        return False
    return True


_LEGACY_ORG_MODELS = {
    "ORGANIZATION": Organization,
    "DIRECTORATE": Directorate,
    "UNIT": Unit,
    "DEPARTMENT": Department,
    "SECTION": Section,
    "DIVISION": Division,
    "TEAM": Team,
}


def _legacy_assignment_org_node(assignment: OrgUnitAssignment) -> OrgNode | None:
    """Map a Portal/HR assignment to its active canonical organization node."""
    return _legacy_unit_org_node(assignment.unit_type, assignment.unit_id)


def _legacy_unit_org_node(unit_type, unit_id) -> OrgNode | None:
    """Map one legacy unit identity to its active canonical organization node."""
    unit_type = (unit_type or "").strip().upper()
    unit_id = int(unit_id or 0)
    if not unit_type or not unit_id:
        return None

    direct_node = (
        OrgNode.query
        .filter_by(
            legacy_type=unit_type,
            legacy_id=unit_id,
            is_active=True,
        )
        .order_by(OrgNode.id.asc())
        .first()
    )
    if direct_node:
        return direct_node

    source_model = _LEGACY_ORG_MODELS.get(unit_type)
    source = db.session.get(source_model, unit_id) if source_model else None
    if not source or not bool(getattr(source, "is_active", True)):
        return None
    return find_approved_org_node_by_name(
        getattr(source, "name_ar", None) or getattr(source, "name_en", None),
        unit_type,
    )


def _legacy_unit_parent(unit_type: str, unit_id: int) -> tuple[str | None, int | None]:
    """Resolve the parent identity for every supported legacy hierarchy level."""
    source_model = _LEGACY_ORG_MODELS.get((unit_type or "").strip().upper())
    source = db.session.get(source_model, int(unit_id)) if source_model else None
    if not source:
        return None, None

    unit_type = (unit_type or "").strip().upper()
    parent_fields = {
        "TEAM": (("DIVISION", "division_id"), ("SECTION", "section_id")),
        "DIVISION": (("SECTION", "section_id"), ("DEPARTMENT", "department_id")),
        "SECTION": (
            ("DEPARTMENT", "department_id"),
            ("UNIT", "unit_id"),
            ("DIRECTORATE", "directorate_id"),
        ),
        "DEPARTMENT": (("UNIT", "unit_id"), ("DIRECTORATE", "directorate_id")),
        "UNIT": (("ORGANIZATION", "organization_id"),),
        "DIRECTORATE": (("ORGANIZATION", "organization_id"),),
    }
    for parent_type, field_name in parent_fields.get(unit_type, ()):
        parent_id = getattr(source, field_name, None)
        if parent_id:
            return parent_type, int(parent_id)
    return None, None


def _legacy_assignment_chain(assignment: OrgUnitAssignment) -> list[tuple[str, int]]:
    """Return a bottom-up chain for one Portal/HR organizational assignment."""
    unit_type = (assignment.unit_type or "").strip().upper()
    unit_id = int(assignment.unit_id or 0)
    chain: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    while unit_type and unit_id and (unit_type, unit_id) not in seen:
        seen.add((unit_type, unit_id))
        chain.append((unit_type, unit_id))
        parent_type, parent_id = _legacy_unit_parent(unit_type, unit_id)
        unit_type, unit_id = parent_type or "", int(parent_id or 0)
    return chain


def _legacy_unit_label(unit_type: str, unit_id: int) -> str:
    source_model = _LEGACY_ORG_MODELS.get((unit_type or "").strip().upper())
    source = db.session.get(source_model, int(unit_id)) if source_model else None
    return (
        getattr(source, "name_ar", None)
        or getattr(source, "name_en", None)
        or f"{unit_type} #{unit_id}"
    )


def _legacy_manager_for_assignment(
    requester: User,
    assignment: OrgUnitAssignment,
) -> tuple[User | None, str | None, str | None, int | None, str]:
    """Find the first non-self manager from the assignment's legacy chain."""
    for unit_type, unit_id in _legacy_assignment_chain(assignment):
        manager_row = (
            OrgUnitManager.query
            .filter(
                db.func.upper(OrgUnitManager.unit_type) == unit_type,
                OrgUnitManager.unit_id == unit_id,
            )
            .first()
        )
        if not manager_row:
            continue
        for user_id, role in (
            (manager_row.manager_user_id, "مسؤول"),
            (manager_row.deputy_user_id, "نائب المسؤول"),
        ):
            if not user_id or int(user_id) == int(requester.id):
                continue
            user = db.session.get(User, int(user_id))
            if not user:
                continue
            canonical_node = _legacy_unit_org_node(unit_type, unit_id)
            return (
                user,
                role,
                _legacy_unit_label(unit_type, unit_id),
                int(canonical_node.id) if canonical_node else None,
                node_path_label(canonical_node) if canonical_node else "",
            )
    return None, None, None, None, ""


def _legacy_manager_candidates_for_assignment(
    requester: User,
    assignment: OrgUnitAssignment,
) -> list[tuple[User, str, str, int | None, str]]:
    """Return every non-self manager/deputy in a legacy assignment path."""
    candidates: list[tuple[User, str, str, int | None, str]] = []
    seen_user_ids: set[int] = set()
    for unit_type, unit_id in _legacy_assignment_chain(assignment):
        if unit_type == "ORGANIZATION":
            continue
        manager_row = (
            OrgUnitManager.query
            .filter(
                db.func.upper(OrgUnitManager.unit_type) == unit_type,
                OrgUnitManager.unit_id == unit_id,
            )
            .first()
        )
        if not manager_row:
            continue
        canonical_node = _legacy_unit_org_node(unit_type, unit_id)
        for user_id, role in (
            (manager_row.manager_user_id, "المسؤول"),
            (manager_row.deputy_user_id, "نائب المسؤول"),
        ):
            if not user_id or int(user_id) in seen_user_ids or int(user_id) == int(requester.id):
                continue
            user = db.session.get(User, int(user_id))
            if not user:
                continue
            seen_user_ids.add(int(user_id))
            candidates.append((
                user,
                role,
                _legacy_unit_label(unit_type, unit_id),
                int(canonical_node.id) if canonical_node else None,
                node_path_label(canonical_node) if canonical_node else "",
            ))
    return candidates


def _requester_assignment_nodes(requester: User) -> list[tuple[OrgNode, bool]]:
    """Return active staff and managerial placements for manager resolution."""
    requester_id = int(requester.id)
    primary_node_id = resolve_user_org_node_id(requester)
    placements: list[tuple[OrgNode, bool]] = []
    seen_node_ids: set[int] = set()

    def add_node(node: OrgNode | None, is_primary: bool = False) -> None:
        if not node or not bool(getattr(node, "is_active", False)):
            return
        node_id = int(node.id)
        if node_id in seen_node_ids:
            if is_primary:
                for index, (existing_node, existing_primary) in enumerate(placements):
                    if int(existing_node.id) == node_id and not existing_primary:
                        placements[index] = (existing_node, True)
                        break
            return
        seen_node_ids.add(node_id)
        placements.append((node, bool(is_primary)))

    if primary_node_id:
        add_node(db.session.get(OrgNode, int(primary_node_id)), True)

    assignments = (
        OrgNodeAssignment.query
        .join(OrgNode, OrgNodeAssignment.node_id == OrgNode.id)
        .filter(
            OrgNodeAssignment.user_id == requester_id,
            OrgNode.is_active.is_(True),
        )
        .order_by(OrgNodeAssignment.is_primary.desc(), OrgNodeAssignment.id.asc())
        .all()
    )
    for assignment in assignments:
        add_node(
            assignment.node,
            bool(assignment.is_primary or int(assignment.node_id) == int(primary_node_id or 0)),
        )

    managerial_assignments = (
        OrgNodeManager.query
        .join(OrgNode, OrgNodeManager.node_id == OrgNode.id)
        .filter(
            OrgNode.is_active.is_(True),
            (OrgNodeManager.manager_user_id == requester_id)
            | (OrgNodeManager.deputy_user_id == requester_id),
        )
        .order_by(OrgNodeManager.node_id.asc())
        .all()
    )
    for assignment in managerial_assignments:
        add_node(
            assignment.node,
            bool(int(assignment.node_id) == int(primary_node_id or 0)),
        )

    legacy_assignments = (
        OrgUnitAssignment.query
        .filter(
            OrgUnitAssignment.user_id == requester_id,
            db.func.upper(OrgUnitAssignment.unit_type) != "TEAM",
        )
        .order_by(OrgUnitAssignment.is_primary.desc(), OrgUnitAssignment.id.asc())
        .all()
    )
    for assignment in legacy_assignments:
        node = _legacy_assignment_org_node(assignment)
        add_node(
            node,
            bool(
                assignment.is_primary
                or (node and int(node.id) == int(primary_node_id or 0))
            ),
        )

    legacy_managerial_assignments = (
        OrgUnitManager.query
        .filter(
            (OrgUnitManager.manager_user_id == requester_id)
            | (OrgUnitManager.deputy_user_id == requester_id),
        )
        .order_by(OrgUnitManager.unit_type.asc(), OrgUnitManager.unit_id.asc())
        .all()
    )
    for assignment in legacy_managerial_assignments:
        node = _legacy_unit_org_node(assignment.unit_type, assignment.unit_id)
        add_node(
            node,
            bool(node and int(node.id) == int(primary_node_id or 0)),
        )

    placements.sort(
        key=lambda placement: (
            0 if placement[1] else 1,
            (_node_type_name(placement[0]) or ""),
            placement[0].name_ar or "",
            int(placement[0].id),
        )
    )
    return placements


def _add_requester_manager_option(
    options_by_user_id: dict[int, dict],
    manager: User,
    *,
    manager_role: str,
    manager_node_id: int | None,
    manager_node_name: str,
    manager_node_label: str,
    assignment_node_id: int | None,
    assignment_label: str,
    is_primary: bool,
) -> None:
    """Merge one manager source into the requester-facing option list."""
    manager_id = int(manager.id)
    option = options_by_user_id.get(manager_id)
    if option:
        option["is_primary"] = bool(option["is_primary"] or is_primary)
        if assignment_label and assignment_label not in option["assignment_labels"]:
            option["assignment_labels"].append(assignment_label)
        return

    options_by_user_id[manager_id] = {
        "user_id": manager_id,
        "name": manager.full_name or manager.email or f"مستخدم #{manager_id}",
        "job_title": (getattr(manager, "job_title", None) or "").strip(),
        "manager_role": manager_role,
        "manager_node_id": manager_node_id,
        "manager_node_name": manager_node_name,
        "manager_node_label": manager_node_label,
        "assignment_node_id": assignment_node_id,
        "assignment_labels": [assignment_label] if assignment_label else [],
        "is_primary": bool(is_primary),
    }


def requester_dynamic_manager_options(requester: User) -> list[dict]:
    """Resolve every active responsible manager for a requester.

    The result combines the employee-file manager, active secondments, and
    manager/deputy assignments across all organizational placements. Duplicate
    people are collapsed into one deterministic choice.
    """
    options_by_user_id: dict[int, dict] = {}

    employee_file = db.session.get(EmployeeFile, int(requester.id))
    if employee_file and employee_file.direct_manager_user_id:
        manager = db.session.get(User, int(employee_file.direct_manager_user_id))
        if manager and int(manager.id) != int(requester.id):
            _add_requester_manager_option(
                options_by_user_id,
                manager,
                manager_role="المسؤول المباشر",
                manager_node_id=None,
                manager_node_name="ملف الموظف",
                manager_node_label="",
                assignment_node_id=None,
                assignment_label="بيانات الموظف",
                is_primary=True,
            )

    today = date.today().isoformat()
    secondments = (
        EmployeeSecondment.query
        .filter_by(user_id=int(requester.id))
        .order_by(EmployeeSecondment.date_from.desc(), EmployeeSecondment.id.asc())
        .all()
    )
    for secondment in secondments:
        if (secondment.date_from and secondment.date_from > today) or (
            secondment.date_to and secondment.date_to < today
        ):
            continue
        if not secondment.direct_manager_user_id:
            continue
        manager = db.session.get(User, int(secondment.direct_manager_user_id))
        if not manager or int(manager.id) == int(requester.id):
            continue
        _add_requester_manager_option(
            options_by_user_id,
            manager,
            manager_role="مسؤول التكليف",
            manager_node_id=None,
            manager_node_name="تكليف",
            manager_node_label="",
            assignment_node_id=None,
            assignment_label="تكليف ساري",
            is_primary=False,
        )
    for assignment_node, is_primary in _requester_assignment_nodes(requester):
        manager = None
        manager_role = None
        manager_node = None
        for node in reversed(node_chain(assignment_node.id)):
            candidate, role = _manager_for_node(node)
            if not candidate or int(candidate.id) == int(requester.id):
                continue
            manager = candidate
            manager_role = role or "مسؤول"
            manager_node = node
            break
        if not manager or not manager_node:
            continue

        manager_id = int(manager.id)
        option = options_by_user_id.get(manager_id)
        assignment_label = node_path_label(assignment_node)
        if option:
            option["is_primary"] = bool(option["is_primary"] or is_primary)
            if assignment_label and assignment_label not in option["assignment_labels"]:
                option["assignment_labels"].append(assignment_label)
            continue
        options_by_user_id[manager_id] = {
            "user_id": manager_id,
            "name": manager.full_name or manager.email or f"مستخدم #{manager_id}",
            "job_title": (getattr(manager, "job_title", None) or "").strip(),
            "manager_role": manager_role,
            "manager_node_id": int(manager_node.id),
            "manager_node_name": manager_node.name_ar or "",
            "manager_node_label": node_path_label(manager_node),
            "assignment_node_id": int(assignment_node.id),
            "assignment_labels": [assignment_label] if assignment_label else [],
            "is_primary": bool(is_primary),
        }

    for assignment_node, is_primary in _requester_assignment_nodes(requester):
        assignment_label = node_path_label(assignment_node)
        for node in reversed(node_chain(assignment_node.id)):
            if _node_type_code(node) in {"ORGANIZATION", "CHAIRPERSON", "SECRETARY_GENERAL"}:
                continue
            if _is_secretary_general_node(node):
                continue
            for manager, manager_role in _manager_candidates_for_node(node):
                if int(manager.id) == int(requester.id):
                    continue
                _add_requester_manager_option(
                    options_by_user_id,
                    manager,
                    manager_role=manager_role,
                    manager_node_id=int(node.id),
                    manager_node_name=node.name_ar or "",
                    manager_node_label=node_path_label(node),
                    assignment_node_id=int(assignment_node.id),
                    assignment_label=assignment_label,
                    is_primary=is_primary,
                )

    # The approved hierarchy can intentionally be locked against legacy sync.
    # In that case secondary Portal/HR assignments may have valid managers in
    # OrgUnitManager without a usable OrgNode mirror. Read every legacy branch
    # directly as a fallback instead of silently keeping only the primary one.
    legacy_assignments = (
        OrgUnitAssignment.query
        .filter(
            OrgUnitAssignment.user_id == int(requester.id),
            db.func.upper(OrgUnitAssignment.unit_type) != "TEAM",
        )
        .order_by(OrgUnitAssignment.is_primary.desc(), OrgUnitAssignment.id.asc())
        .all()
    )
    for assignment in legacy_assignments:
        manager, manager_role, manager_unit_name, manager_node_id, manager_node_label = (
            _legacy_manager_for_assignment(requester, assignment)
        )
        if not manager:
            continue
        manager_id = int(manager.id)
        assignment_node = _legacy_assignment_org_node(assignment)
        assignment_label = (
            node_path_label(assignment_node)
            if assignment_node else
            _legacy_unit_label(assignment.unit_type, assignment.unit_id)
        )
        option = options_by_user_id.get(manager_id)
        if option:
            option["is_primary"] = bool(option["is_primary"] or assignment.is_primary)
            if assignment_label and assignment_label not in option["assignment_labels"]:
                option["assignment_labels"].append(assignment_label)
            continue
        options_by_user_id[manager_id] = {
            "user_id": manager_id,
            "name": manager.full_name or manager.email or f"مستخدم #{manager_id}",
            "job_title": (getattr(manager, "job_title", None) or "").strip(),
            "manager_role": manager_role or "مسؤول",
            "manager_node_id": manager_node_id,
            "manager_node_name": manager_unit_name or "",
            "manager_node_label": manager_node_label,
            "assignment_node_id": int(assignment_node.id) if assignment_node else None,
            "assignment_labels": [assignment_label] if assignment_label else [],
            "is_primary": bool(assignment.is_primary),
        }

    for assignment in legacy_assignments:
        assignment_node = _legacy_assignment_org_node(assignment)
        assignment_label = (
            node_path_label(assignment_node)
            if assignment_node else
            _legacy_unit_label(assignment.unit_type, assignment.unit_id)
        )
        for manager, manager_role, manager_unit_name, manager_node_id, manager_node_label in (
            _legacy_manager_candidates_for_assignment(requester, assignment)
        ):
            _add_requester_manager_option(
                options_by_user_id,
                manager,
                manager_role=manager_role,
                manager_node_id=manager_node_id,
                manager_node_name=manager_unit_name or "",
                manager_node_label=manager_node_label,
                assignment_node_id=int(assignment_node.id) if assignment_node else None,
                assignment_label=assignment_label,
                is_primary=bool(assignment.is_primary),
            )

    return sorted(
        options_by_user_id.values(),
        key=lambda option: (
            0 if option["is_primary"] else 1,
            option["name"],
            option["user_id"],
        ),
    )


def org_node_approver_names(node_ids=None) -> dict[int, str]:
    """Return the currently assigned manager/deputy names for OrgNodes.

    A predefined ``ORG_NODE`` step is resolved from the live hierarchy, so its
    display should identify the same people instead of a generic placeholder.
    """
    query = OrgNodeManager.query
    if node_ids is not None:
        normalized_ids = {
            int(node_id)
            for node_id in node_ids
            if node_id is not None and str(node_id).isdigit()
        }
        if not normalized_ids:
            return {}
        query = query.filter(OrgNodeManager.node_id.in_(normalized_ids))

    result: dict[int, str] = {}
    for assignment in query.all():
        names = []
        for user in (assignment.manager_user, assignment.deputy_user):
            if not user:
                continue
            name = (user.full_name or user.email or "").strip()
            if name and name not in names:
                names.append(name)
        if names:
            result[int(assignment.node_id)] = " / ".join(names)
    return result


def build_structural_template_path(source_node_id: int, target_node_id: int) -> dict:
    """Build reusable ORG_NODE step specs from two nodes in the saved hierarchy."""
    source = db.session.get(OrgNode, int(source_node_id)) if source_node_id else None
    target = db.session.get(OrgNode, int(target_node_id)) if target_node_id else None
    if not source or not target:
        return {"steps": [], "warnings": [], "errors": ["يرجى اختيار نقطتي بداية ونهاية صحيحتين."]}

    steps = []
    skipped = []
    for node in structural_route_nodes(source.id, target.id):
        if not _node_allows_approval(node):
            continue
        manager, _manager_role = _manager_for_node(node)
        if not manager:
            skipped.append(node.name_ar)
            continue
        steps.append({
            "approver_kind": "ORG_NODE",
            "approver_org_node_id": int(node.id),
            "label": node_path_label(node),
        })

    errors = []
    if not steps:
        errors.append("لا يوجد مسؤول أو نائب مسؤول معيّن على عناصر هذا المسار الهيكلي.")
    warnings = []
    if skipped:
        warnings.append("تم تجاوز عناصر بلا مسؤول معيّن: " + "، ".join(skipped[:8]))
    return {"steps": steps, "warnings": warnings, "errors": errors}


def dynamic_user_choices(
    requester: User,
    *,
    include_unassigned: bool = False,
) -> list[dict]:
    """Return people selectable for a dynamic route.

    The General Secretary may use direct delivery, which also permits an
    employee who has not yet been placed in the organizational tree.  Those
    employees remain hidden in the normal structural-routing experience.
    """
    choices = []
    requester_chain = node_chain(resolve_user_org_node_id(requester))
    memberships_by_user: dict[int, list[dict]] = {}
    membership_rows = (
        TeamMembership.query
        .join(Team, TeamMembership.team_id == Team.id)
        .filter(
            TeamMembership.is_active.is_(True),
            Team.is_active.is_(True),
        )
        .order_by(Team.name_ar.asc(), TeamMembership.id.asc())
        .all()
    )
    for membership in membership_rows:
        memberships_by_user.setdefault(int(membership.user_id), []).append({
            "id": int(membership.team_id),
            "key": f"T{int(membership.team_id)}",
            "name": membership.team.name_ar,
            "title": (membership.title or "").strip(),
        })

    for user in User.query.order_by(User.name.asc(), User.email.asc(), User.id.asc()).all():
        if int(user.id) == int(requester.id):
            continue
        node_id = resolve_user_org_node_id(user)
        chain = node_chain(node_id)
        if not chain and not include_unassigned:
            continue
        team_node = next(
            (node for node in reversed(chain) if _node_type_code(node) == "TEAM"),
            None,
        )
        user_teams = list(memberships_by_user.get(int(user.id), []))
        if team_node and not user_teams:
            user_teams.append({
                "id": int(team_node.id),
                "key": f"N{int(team_node.id)}",
                "name": team_node.name_ar,
                "title": "",
            })
        first_team = user_teams[0] if user_teams else None
        choices.append({
            "id": int(user.id),
            "name": user.full_name or user.email or f"مستخدم #{user.id}",
            "job_title": (getattr(user, "job_title", None) or "").strip(),
            "email": user.email or "",
            "node_id": int(node_id) if node_id else None,
            "node_label": node_path_label(node_id) if node_id else "غير مربوط بالهيكل التنظيمي",
            "team_id": first_team["id"] if first_team else None,
            "team_name": first_team["name"] if first_team else "",
            "teams": user_teams,
            "team_keys": [team["key"] for team in user_teams],
            "team_names": [team["name"] for team in user_teams],
            "same_administration": same_administration(requester_chain, chain),
        })
    return choices


def _normalize_committee_delivery_mode(value: str | None) -> tuple[str, str, str] | None:
    raw = (value or "ALL").strip().upper()
    if raw.startswith("COMMITTEE_"):
        raw = raw.split("_", 1)[1]
    configured = COMMITTEE_DELIVERY_MODES.get(raw)
    if not configured:
        return None
    canonical, label = configured
    return raw, canonical, label


def _committee_assignees_for_mode(
    committee: Committee,
    mode_key: str,
) -> list[CommitteeAssignee]:
    active_assignees = [
        assignee
        for assignee in (committee.assignees or [])
        if bool(getattr(assignee, "is_active", False))
    ]
    if mode_key == "CHAIR":
        return [
            assignee for assignee in active_assignees
            if (assignee.member_role or "").strip().upper() == "CHAIR"
        ]
    if mode_key == "SECRETARY":
        return [
            assignee for assignee in active_assignees
            if (assignee.member_role or "").strip().upper() == "SECRETARY"
        ]
    return active_assignees


def dynamic_committee_choices() -> list[dict]:
    """Return active committees and delivery modes available for dynamic routing."""
    committees = (
        Committee.query
        .filter(Committee.is_active.is_(True))
        .order_by(Committee.name_ar.asc(), Committee.id.asc())
        .all()
    )
    choices = []
    for committee in committees:
        available_modes = []
        for mode_key, (canonical, label) in COMMITTEE_DELIVERY_MODES.items():
            assignees = _committee_assignees_for_mode(committee, mode_key)
            if assignees:
                available_modes.append({
                    "key": mode_key,
                    "value": canonical,
                    "label": label,
                    "assignee_count": len(assignees),
                })
        all_mode = next(
            (mode for mode in available_modes if mode["key"] == "ALL"),
            None,
        )
        choices.append({
            "id": int(committee.id),
            "name": committee.label,
            "code": (committee.code or "").strip(),
            "can_select": bool(all_mode),
            "member_count": int(all_mode["assignee_count"]) if all_mode else 0,
            "available_modes": available_modes,
            "unavailable_reason": (
                "لا يوجد أعضاء نشطون في هذه اللجنة."
                if not all_mode else ""
            ),
        })
    return choices


def dynamic_org_browser_nodes(choices: list[dict], requester: User | None = None) -> list[dict]:
    """Return active hierarchy nodes with their selectable manager context."""
    nodes = (
        OrgNode.query
        .join(OrgNode.type)
        .filter(
            OrgNode.is_active.is_(True),
            OrgNodeType.is_active.is_(True),
        )
        .order_by(
            OrgNodeType.sort_order.asc(),
            OrgNode.sort_order.asc(),
            OrgNode.name_ar.asc(),
            OrgNode.id.asc(),
        )
        .all()
    )
    nodes_by_id = {int(node.id): node for node in nodes}
    direct_counts: dict[int, int] = {}
    for choice in choices:
        node_id = int(choice.get("node_id") or 0)
        if node_id in nodes_by_id:
            direct_counts[node_id] = direct_counts.get(node_id, 0) + 1

    children_by_parent: dict[int | None, list[int]] = {}
    for node in nodes:
        parent_id = int(node.parent_id) if node.parent_id in nodes_by_id else None
        children_by_parent.setdefault(parent_id, []).append(int(node.id))

    total_counts: dict[int, int] = {}
    requester_chain = node_chain(resolve_user_org_node_id(requester)) if requester else []

    def selectable_count(node_id: int, active_path: set[int] | None = None) -> int:
        if node_id in total_counts:
            return total_counts[node_id]
        active_path = set(active_path or ())
        if node_id in active_path:
            return direct_counts.get(node_id, 0)
        active_path.add(node_id)
        total = direct_counts.get(node_id, 0)
        total += sum(
            selectable_count(child_id, active_path)
            for child_id in children_by_parent.get(node_id, [])
        )
        total_counts[node_id] = total
        return total

    manager_context = {
        int(node.id): _manager_for_node(node)
        for node in nodes
    }
    result = []
    for node in nodes:
        node_id = int(node.id)
        employee_count = selectable_count(node_id)
        manager, manager_role = manager_context[node_id]
        has_manager = bool(manager and _node_allows_approval(node))
        is_implicit_only_level = bool(
            _node_type_code(node) in {"ORGANIZATION", "CHAIRPERSON", "SECRETARY_GENERAL"}
            or _is_secretary_general_node(node)
        )
        result.append({
            "id": node_id,
            "parent_id": int(node.parent_id) if node.parent_id in nodes_by_id else None,
            "name": node.name_ar,
            "type_name": _node_type_name(node) or _node_type_code(node),
            "node_label": node_path_label(node),
            "direct_user_count": direct_counts.get(node_id, 0),
            "total_user_count": employee_count,
            "can_select": bool(has_manager and not is_implicit_only_level),
            "has_manager": has_manager,
            "unavailable_reason": (
                "يُضاف هذا المستوى القيادي فقط من خياره المستقل."
                if is_implicit_only_level else
                "لا يوجد مسؤول أو نائب مسؤول معتمد لهذه الجهة."
                if not has_manager else
                ""
            ),
            "manager_user_id": int(manager.id) if manager else None,
            "manager_name": (
                manager.full_name or manager.email or f"مستخدم #{manager.id}"
                if manager else ""
            ),
            "manager_job_title": (
                (getattr(manager, "job_title", None) or "").strip()
                if manager else ""
            ),
            "manager_role": manager_role or "",
            "same_administration": same_administration(requester_chain, node_chain(node_id)),
        })
    return result


def _normalized_user_ids(values) -> tuple[list[int], list[str]]:
    user_ids = []
    errors = []
    seen = set()
    for raw_value in values or []:
        try:
            user_id = int(raw_value)
        except (TypeError, ValueError):
            errors.append("قائمة الأشخاص المختارين تحتوي على قيمة غير صالحة.")
            continue
        if user_id in seen:
            errors.append("لا يمكن تكرار الشخص نفسه ضمن خطوات المسار الديناميكي.")
            continue
        seen.add(user_id)
        user_ids.append(user_id)
    if len(user_ids) > MAX_DYNAMIC_TARGETS:
        errors.append(f"يمكن اختيار {MAX_DYNAMIC_TARGETS} شخصاً كحد أقصى للمسار الديناميكي.")
    return user_ids[:MAX_DYNAMIC_TARGETS], errors


def build_dynamic_user_path(requester: User, selected_user_ids) -> dict:
    """Expand ordered user choices into safe runtime USER steps."""
    user_ids, errors = _normalized_user_ids(selected_user_ids)
    if not user_ids:
        errors.append("اختر شخصاً واحداً على الأقل للمسار الديناميكي.")

    users = User.query.filter(User.id.in_(user_ids)).all() if user_ids else []
    users_map = {int(user.id): user for user in users}
    missing_users = [user_id for user_id in user_ids if user_id not in users_map]
    if missing_users:
        errors.append("تعذر العثور على بعض الأشخاص المختارين.")

    requester_node_id = resolve_user_org_node_id(requester)
    requester_chain = node_chain(requester_node_id)
    if not requester_chain:
        errors.append("يجب ربط منشئ الطلب بعنصر أساسي في الهيكل التنظيمي أولاً.")

    steps: list[dict] = []
    warnings: list[str] = []
    segments: list[dict] = []
    seen_approver_user_ids: set[int] = {int(requester.id)}

    def add_user_step(user: User, reason: str, node: OrgNode | None = None) -> bool:
        user_id = int(user.id)
        if user_id in seen_approver_user_ids:
            return False
        steps.append({
            "step_order": len(steps) + 1,
            "mode": "SEQUENTIAL",
            "approver_kind": "USER",
            "approver_user_id": user_id,
            "sla_days": None,
            "label": user.full_name or user.email or f"مستخدم #{user.id}",
            "job_title": (getattr(user, "job_title", None) or "").strip(),
            "reason": reason,
            "node_id": int(node.id) if node else None,
            "node_label": node_path_label(node) if node else "",
        })
        seen_approver_user_ids.add(user_id)
        return True

    current_user = requester
    current_chain = requester_chain
    for target_user_id in user_ids:
        target_user = users_map.get(target_user_id)
        if not target_user:
            continue
        if int(target_user.id) == int(requester.id):
            errors.append("لا يمكن اختيار منشئ الطلب كخطوة اعتماد لنفس الطلب.")
            continue

        target_node_id = resolve_user_org_node_id(target_user)
        target_chain = node_chain(target_node_id)
        if not target_chain:
            errors.append(f"المستخدم «{target_user.full_name}» غير مربوط بالهيكل التنظيمي.")
            continue

        direct = same_administration(current_chain, target_chain)
        segment = {
            "from_user_id": int(current_user.id),
            "to_user_id": int(target_user.id),
            "same_administration": direct,
            "intermediate_manager_count": 0,
        }

        route_nodes = vertical_structural_route_nodes(current_chain, target_chain)
        resolved_manager_count = 0
        skipped_nodes = []
        for node in route_nodes:
            if not _node_allows_approval(node):
                continue
            manager, manager_role = _manager_for_node(node)
            if not manager:
                skipped_nodes.append(node.name_ar)
                continue
            resolved_manager_count += 1
            if add_user_step(
                manager,
                f"{manager_role} «{node.name_ar}» ضمن المسار العمودي",
                node,
            ):
                segment["intermediate_manager_count"] += 1

        if not resolved_manager_count and not direct:
            errors.append(
                f"لا يمكن الانتقال من «{current_user.full_name}» إلى «{target_user.full_name}»: "
                "لم يتم تعيين مسؤول أو نائب مسؤول على المسار العمودي بينهما."
            )
        elif skipped_nodes:
            warnings.append(
                "تم تجاوز عناصر بلا مسؤول معيّن بين "
                f"«{current_user.full_name}» و«{target_user.full_name}»: "
                + "، ".join(skipped_nodes[:8])
            )

        add_user_step(
            target_user,
            "المستلم المختار بعد المرور بالمسار الإداري العمودي",
            target_chain[-1],
        )

        segments.append(segment)
        current_user = target_user
        current_chain = target_chain

    for index, step in enumerate(steps, start=1):
        step["step_order"] = index

    return {
        "origin": route_origin(requester, requester_chain),
        "steps": steps,
        "segments": segments,
        "warnings": warnings,
        "errors": list(dict.fromkeys(errors)),
    }


def _normalized_dynamic_target_refs(
    values,
) -> tuple[list[tuple[str, int, int | None, str | None]], list[str]]:
    targets: list[tuple[str, int, int | None, str | None]] = []
    errors: list[str] = []
    seen: set[tuple[str, int]] = set()
    for raw_value in values or []:
        value = str(raw_value or "").strip().upper()
        if not value:
            continue
        kind = "USER"
        raw_id = value
        if ":" in value:
            raw_kind, raw_id = value.split(":", 1)
            kind = {
                "U": "USER",
                "USER": "USER",
                "N": "NODE",
                "NODE": "NODE",
                "C": "COMMITTEE",
                "COMMITTEE": "COMMITTEE",
            }.get(raw_kind, "")
        route_start_id = None
        committee_delivery_mode = None
        if "@" in raw_id:
            raw_id, raw_suffix = raw_id.split("@", 1)
            if kind == "NODE" and raw_suffix.isdigit():
                route_start_id = int(raw_suffix)
            elif kind == "COMMITTEE":
                normalized_mode = _normalize_committee_delivery_mode(raw_suffix)
                if not normalized_mode:
                    errors.append("طريقة تسليم اللجنة المختارة غير صالحة.")
                    continue
                committee_delivery_mode = normalized_mode[0]
            else:
                errors.append("نقطة بدء التسلسل الإداري أو طريقة تسليم اللجنة غير صالحة.")
                continue
        elif kind == "COMMITTEE":
            committee_delivery_mode = "ALL"
        if kind not in {"USER", "NODE", "COMMITTEE"} or not raw_id.isdigit():
            errors.append("قائمة الجهات أو الأشخاص أو اللجان المختارة تحتوي على قيمة غير صالحة.")
            continue
        target_key = (kind, int(raw_id))
        if target_key in seen:
            errors.append("لا يمكن تكرار الجهة أو الشخص أو اللجنة نفسها ضمن خطوات المسار الديناميكي.")
            continue
        seen.add(target_key)
        targets.append((kind, int(raw_id), route_start_id, committee_delivery_mode))
    committee_indexes = [
        index for index, target in enumerate(targets)
        if target[0] == "COMMITTEE"
    ]
    if len(committee_indexes) > 1:
        errors.append("يمكن اختيار لجنة واحدة فقط كوجهة للمسار الديناميكي.")
    if committee_indexes and committee_indexes[-1] != len(targets) - 1:
        errors.append("يجب أن تكون اللجنة آخر وجهة مختارة في المسار الديناميكي.")
    if len(targets) > MAX_DYNAMIC_TARGETS:
        errors.append(f"يمكن اختيار {MAX_DYNAMIC_TARGETS} جهة أو شخصاً أو لجنة كحد أقصى للمسار الديناميكي.")
    return targets[:MAX_DYNAMIC_TARGETS], errors


def build_dynamic_target_path(
    requester: User,
    selected_target_refs,
    include_secretary_general: bool = False,
    sla_days: int | None = None,
    selected_manager_user_ids=None,
    delivery_mode: str = DYNAMIC_DELIVERY_MODE_HIERARCHICAL,
) -> dict:
    """Expand ordered USER/NODE/COMMITTEE targets into runtime steps.

    Normal routes pass through the vertical organizational hierarchy.  The
    General Secretary can instead choose direct delivery, which sends the
    selected USER targets in their chosen order without inserting managers or
    a return path.
    """
    target_refs, errors = _normalized_dynamic_target_refs(selected_target_refs)
    if not target_refs:
        errors.append("اختر جهة تنظيمية أو شخصاً أو لجنة واحدة على الأقل للمسار الديناميكي.")

    normalized_delivery_mode = _normalize_dynamic_delivery_mode(delivery_mode)
    if not normalized_delivery_mode:
        errors.append("طريقة تسليم المسار الديناميكي غير صالحة.")
        normalized_delivery_mode = DYNAMIC_DELIVERY_MODE_HIERARCHICAL
    direct_delivery = normalized_delivery_mode == DYNAMIC_DELIVERY_MODE_DIRECT
    if direct_delivery and not can_use_direct_dynamic_delivery(requester):
        errors.append("التوجيه المباشر دون التسلسل الإداري متاح لدور الأمين العام فقط.")
        direct_delivery = False
        normalized_delivery_mode = DYNAMIC_DELIVERY_MODE_HIERARCHICAL

    user_ids = [target_id for kind, target_id, _start_id, _mode in target_refs if kind == "USER"]
    node_ids = [target_id for kind, target_id, _start_id, _mode in target_refs if kind == "NODE"]
    committee_ids = [
        target_id for kind, target_id, _start_id, _mode in target_refs
        if kind == "COMMITTEE"
    ]
    users_map = {
        int(user.id): user
        for user in (User.query.filter(User.id.in_(user_ids)).all() if user_ids else [])
    }
    nodes_map = {
        int(node.id): node
        for node in (OrgNode.query.filter(OrgNode.id.in_(node_ids)).all() if node_ids else [])
    }
    committees_map = {
        int(committee.id): committee
        for committee in (
            Committee.query.filter(Committee.id.in_(committee_ids)).all()
            if committee_ids else []
        )
    }

    requester_chain = node_chain(resolve_user_org_node_id(requester))
    manager_options = requester_dynamic_manager_options(requester)
    manager_options_by_id = {
        int(option["user_id"]): option
        for option in manager_options
    }
    explicit_manager_selection = selected_manager_user_ids is not None
    if direct_delivery:
        # Direct delivery must never inherit an optional manager selection
        # posted by the browser or another integration.
        selected_manager_ids: list[int] = []
    elif explicit_manager_selection:
        normalized_manager_ids: list[int] = []
        for raw_user_id in selected_manager_user_ids or []:
            try:
                user_id = int(raw_user_id)
            except (TypeError, ValueError):
                errors.append("قائمة المديرين المختارين تحتوي على قيمة غير صالحة.")
                continue
            if user_id not in normalized_manager_ids:
                normalized_manager_ids.append(user_id)
        invalid_manager_ids = [
            user_id for user_id in normalized_manager_ids
            if user_id not in manager_options_by_id
        ]
        if invalid_manager_ids:
            errors.append("أحد المديرين المختارين لا يرتبط حالياً بأي تعيين لمنشئ الطلب.")
        selected_manager_ids = [
            user_id for user_id in normalized_manager_ids
            if user_id in manager_options_by_id
        ]
        # Selecting direct managers is optional.  An explicit empty selection
        # means that the request starts from the chosen target immediately.
    else:
        # Calls from older integrations keep the established automatic route.
        # The new-request UI always sends an explicit selection.
        selected_manager_ids = []
    requires_org_chain = bool(
        include_secretary_general
        or (
            not direct_delivery
            and any(kind != "COMMITTEE" for kind, _target_id, _start_id, _mode in target_refs)
        )
    )
    if not requester_chain and requires_org_chain:
        errors.append("يجب ربط منشئ الطلب بعنصر أساسي في الهيكل التنظيمي أولاً.")

    resolved_targets: list[dict] = []
    for kind, target_id, _route_start_id, committee_delivery_mode in target_refs:
        if kind == "COMMITTEE":
            committee = committees_map.get(target_id)
            if not committee or not bool(getattr(committee, "is_active", False)):
                errors.append(f"تعذر العثور على اللجنة المختارة رقم {target_id}.")
                continue
            normalized_mode = _normalize_committee_delivery_mode(committee_delivery_mode)
            if not normalized_mode:
                errors.append(f"طريقة تسليم اللجنة «{committee.label}» غير صالحة.")
                continue
            mode_key, canonical_mode, mode_label = normalized_mode
            assignees = _committee_assignees_for_mode(committee, mode_key)
            if not assignees:
                errors.append(
                    f"لا يوجد مستلم نشط بصفة «{mode_label}» في اللجنة «{committee.label}»."
                )
                continue
            resolved_targets.append({
                "kind": "COMMITTEE",
                "id": target_id,
                "committee": committee,
                "committee_delivery_mode": canonical_mode,
                "committee_mode_key": mode_key,
                "committee_mode_label": mode_label,
                "label": f"لجنة: {committee.label}",
            })
            continue

        if kind == "USER":
            target_user = users_map.get(target_id)
            if not target_user:
                errors.append(f"تعذر العثور على الشخص المختار رقم {target_id}.")
                continue
            if int(target_user.id) == int(requester.id):
                errors.append("لا يمكن اختيار منشئ الطلب كخطوة اعتماد لنفس الطلب.")
                continue
            target_node_id = resolve_user_org_node_id(target_user)
            target_chain = node_chain(target_node_id)
            if not target_chain and not direct_delivery:
                errors.append(f"المستخدم «{target_user.full_name}» غير مربوط بالهيكل التنظيمي.")
                continue
            resolved_targets.append({
                "kind": "USER",
                "id": target_id,
                "user": target_user,
                "node": target_chain[-1] if target_chain else None,
                "chain": target_chain,
                "label": target_user.full_name or target_user.email or f"مستخدم #{target_user.id}",
            })
            continue

        target_node = nodes_map.get(target_id)
        if not target_node or not getattr(target_node, "is_active", False):
            errors.append(f"تعذر العثور على الجهة التنظيمية المختارة رقم {target_id}.")
            continue
        if not _node_allows_approval(target_node):
            errors.append(f"الجهة «{target_node.name_ar}» غير مفعلة ضمن خطوات الاعتماد.")
            continue
        target_manager, manager_role = _manager_for_node(target_node)
        if not target_manager:
            errors.append(f"لا يوجد مسؤول أو نائب مسؤول معيّن للجهة «{target_node.name_ar}».")
            continue
        if int(target_manager.id) == int(requester.id):
            errors.append(f"لا يمكن لمنشئ الطلب اعتماد طلبه بصفته مسؤول الجهة «{target_node.name_ar}».")
            continue
        target_chain = node_chain(target_node.id)
        if not target_chain:
            errors.append(f"تعذر تحديد موقع الجهة «{target_node.name_ar}» في الهيكل التنظيمي.")
            continue
        resolved_targets.append({
            "kind": "NODE",
            "id": target_id,
            "user": target_manager,
            "node": target_node,
            "chain": target_chain,
            "manager_role": manager_role or "مسؤول",
            "label": f"{_node_type_name(target_node) or _node_type_code(target_node)}: {target_node.name_ar}",
        })

    steps: list[dict] = []
    warnings: list[str] = []
    segments: list[dict] = []
    seen_approver_user_ids: set[int] = {int(requester.id)}

    def add_user_step(user: User, reason: str, node: OrgNode | None = None) -> bool:
        user_id = int(user.id)
        if user_id in seen_approver_user_ids:
            return False
        steps.append({
            "step_order": len(steps) + 1,
            "mode": "SEQUENTIAL",
            "approver_kind": "USER",
            "approver_user_id": user_id,
            "approver_org_node_id": int(node.id) if node else None,
            "sla_days": None,
            "label": user.full_name or user.email or f"مستخدم #{user.id}",
            "job_title": (getattr(user, "job_title", None) or "").strip(),
            "reason": reason,
            "node_id": int(node.id) if node else None,
            "node_label": node_path_label(node) if node else "",
        })
        seen_approver_user_ids.add(user_id)
        return True

    def add_target_step(target: dict) -> bool:
        target_user = target["user"]
        target_node = target["node"]
        if target["kind"] == "USER":
            return add_user_step(
                target_user,
                (
                    "توجيه مباشر من الأمين العام دون المرور بالتسلسل الإداري"
                    if direct_delivery else
                    "المستلم المختار بعد المرور بالمسار الإداري العمودي"
                ),
                target_node,
            )
        target_user_id = int(target_user.id)
        if target_user_id in seen_approver_user_ids:
            return False
        steps.append({
            "step_order": len(steps) + 1,
            "mode": "SEQUENTIAL",
            "approver_kind": "ORG_NODE",
            "approver_org_node_id": int(target_node.id),
            "sla_days": None,
            "label": target_user.full_name or target_user.email or target["label"],
            "job_title": (getattr(target_user, "job_title", None) or "").strip(),
            "reason": (
                f"مسؤول الجهة الهدف «{target['label']}» بتوجيه مباشر من الأمين العام"
                if direct_delivery else
                f"مسؤول الجهة الهدف «{target['label']}» ضمن المسار العمودي"
            ),
            "node_id": int(target_node.id),
            "node_label": node_path_label(target_node),
        })
        seen_approver_user_ids.add(target_user_id)
        return True

    def add_committee_step(target: dict) -> None:
        committee = target["committee"]
        steps.append({
            "step_order": len(steps) + 1,
            "mode": "SEQUENTIAL",
            "approver_kind": "COMMITTEE",
            "approver_committee_id": int(committee.id),
            "committee_delivery_mode": target["committee_delivery_mode"],
            "sla_days": None,
            "label": target["label"],
            "job_title": target["committee_mode_label"],
            "reason": "وجهة لجنة مختارة ضمن المسار الديناميكي",
            "node_id": None,
            "node_label": "",
        })

    selectable_manager_ids = set(manager_options_by_id) if explicit_manager_selection else set()
    for manager_user_id in selected_manager_ids:
        option = manager_options_by_id[manager_user_id]
        manager_user = db.session.get(User, manager_user_id)
        manager_node_id = option.get("manager_node_id")
        manager_node = (
            db.session.get(OrgNode, int(manager_node_id))
            if manager_node_id else None
        )
        if not manager_user:
            errors.append("تعذر تحميل أحد المديرين المختارين من الهيكل التنظيمي.")
            continue
        assignment_names = [
            label.rsplit("←", 1)[-1].strip()
            for label in option.get("assignment_labels", [])
            if label
        ]
        assignment_reason = "، ".join(assignment_names)
        add_user_step(
            manager_user,
            (
                f"{option['manager_role']} مختار من تعيينات منشئ الطلب"
                + (f" — {assignment_reason}" if assignment_reason else "")
            ),
            manager_node,
        )

    current_user = requester
    current_chain = requester_chain
    first_structural_target = True
    for target in resolved_targets:
        if target["kind"] == "COMMITTEE":
            target_ref = (
                f"COMMITTEE:{int(target['id'])}@{target['committee_mode_key']}"
            )
            add_committee_step(target)
            segments.append({
                "from_user_id": int(current_user.id),
                "to_user_id": None,
                "target_kind": "COMMITTEE",
                "target_id": int(target["id"]),
                "target_ref": target_ref,
                "route_start_node_id": None,
                "route_start_label": "",
                "same_administration": False,
                "intermediate_manager_count": 0,
            })
            continue

        target_user = target["user"]
        target_node = target["node"]
        target_chain = target["chain"]
        same_admin = same_administration(current_chain, target_chain)
        target_ref = f"{target['kind']}:{int(target['id'])}"
        segment = {
            "from_user_id": int(current_user.id),
            "to_user_id": int(target_user.id),
            "target_kind": target["kind"],
            "target_id": int(target["id"]),
            "target_ref": target_ref,
            "route_start_node_id": None,
            "route_start_label": "",
            "same_administration": same_admin,
            "delivery_mode": normalized_delivery_mode,
            "intermediate_manager_count": 0,
        }

        if direct_delivery:
            add_target_step(target)
            segments.append(segment)
            current_user = target_user
            current_chain = target_chain
            first_structural_target = False
            continue

        route_nodes = vertical_structural_route_nodes(current_chain, target_chain)
        resolved_manager_count = 0
        skipped_nodes = []
        for route_node in route_nodes:
            if target["kind"] == "NODE" and int(route_node.id) == int(target_node.id):
                continue
            if not _node_allows_approval(route_node):
                continue
            manager, manager_role = _manager_for_node(route_node)
            if not manager:
                skipped_nodes.append(route_node.name_ar)
                continue
            resolved_manager_count += 1
            if first_structural_target and int(manager.id) in selectable_manager_ids:
                continue
            if add_user_step(
                manager,
                f"{manager_role} «{route_node.name_ar}» ضمن المسار العمودي",
                route_node,
            ):
                segment["intermediate_manager_count"] += 1

        if not resolved_manager_count and not same_admin:
            errors.append(
                f"لا يمكن الانتقال من «{current_user.full_name}» إلى «{target['label']}»: "
                "لم يتم تعيين مسؤول أو نائب مسؤول على المسار العمودي بينهما."
            )
        elif skipped_nodes:
            warnings.append(
                "تم تجاوز عناصر بلا مسؤول معيّن بين "
                f"«{current_user.full_name}» و«{target['label']}»: "
                + "، ".join(skipped_nodes[:8])
            )
        add_target_step(target)

        segments.append(segment)
        current_user = target_user
        current_chain = target_chain
        first_structural_target = False

    if include_secretary_general:
        final_chain = next(
            (
                target["chain"]
                for target in reversed(resolved_targets)
                if target.get("chain")
            ),
            requester_chain,
        )
        secretary_general = next(
            (node for node in final_chain if _is_secretary_general_node(node)),
            None,
        )
        secretary_manager, _secretary_role = (
            _manager_for_node(secretary_general)
            if secretary_general else
            (None, None)
        )
        if not secretary_general or not _node_allows_approval(secretary_general):
            errors.append("تعذر تحديد مستوى الأمين العام لإضافته كخطوة أخيرة.")
        elif not secretary_manager:
            errors.append("لا يوجد مسؤول معتمد على مستوى الأمين العام لإضافته كخطوة أخيرة.")
        elif int(secretary_manager.id) == int(requester.id):
            errors.append("لا يمكن لمنشئ الطلب اعتماد طلبه بصفته الأمين العام.")
        else:
            steps.append({
                "step_order": len(steps) + 1,
                "mode": "SEQUENTIAL",
                "approver_kind": "ORG_NODE",
                "approver_org_node_id": int(secretary_general.id),
                "sla_days": None,
                "label": secretary_manager.full_name or secretary_manager.email or (
                    f"{_node_type_name(secretary_general) or _node_type_code(secretary_general)}: "
                    f"{secretary_general.name_ar}"
                ),
                "job_title": (getattr(secretary_manager, "job_title", None) or "").strip(),
                "reason": "",
                "node_id": int(secretary_general.id),
                "node_label": node_path_label(secretary_general),
            })

    if not direct_delivery:
        # A hierarchical dynamic route is a round trip.  After the final
        # destination acts, the request returns through the same approvals in
        # reverse order.  The final destination itself is not duplicated;
        # completing the last return step hands it back to its creator.
        forward_steps = list(steps)
        for index in range(len(forward_steps) - 2, -1, -1):
            forward_step = forward_steps[index]
            following_step = forward_steps[index + 1]
            if _steps_are_non_managerial_peers(forward_step, following_step):
                continue
            return_step = dict(forward_step)
            return_step["reason"] = DYNAMIC_RETURN_REASON
            steps.append(return_step)

    try:
        effective_dynamic_sla = int(sla_days) if sla_days is not None else None
        if effective_dynamic_sla is not None and effective_dynamic_sla <= 0:
            effective_dynamic_sla = None
    except (TypeError, ValueError):
        effective_dynamic_sla = None

    for index, step in enumerate(steps, start=1):
        step["step_order"] = index
        if effective_dynamic_sla is not None:
            step["sla_days"] = effective_dynamic_sla

    return {
        "origin": route_origin(requester, requester_chain),
        "steps": steps,
        "segments": segments,
        "warnings": warnings,
        "errors": list(dict.fromkeys(errors)),
        "include_secretary_general": bool(include_secretary_general),
        "delivery_mode": normalized_delivery_mode,
        "manager_options": manager_options,
        "selected_manager_user_ids": selected_manager_ids,
    }
