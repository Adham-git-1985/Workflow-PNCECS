from __future__ import annotations

from extensions import db
from models import (
    Committee,
    CommitteeAssignee,
    OrgNode,
    OrgNodeAssignment,
    OrgNodeManager,
    OrgNodeType,
    Team,
    TeamMembership,
    User,
)
from utils.org_dynamic import resolve_user_org_node_id


MAX_DYNAMIC_TARGETS = 20
DYNAMIC_ROUTE_EXCLUDED_TOP_LEVELS = 2
FINAL_SECRETARY_GENERAL_REF = "FINAL_SECRETARY_GENERAL"
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


def dynamic_route_start_nodes(target_chain: list[OrgNode]) -> list[OrgNode]:
    """Return selectable route starts after the two excluded top hierarchy levels."""
    return list(target_chain[DYNAMIC_ROUTE_EXCLUDED_TOP_LEVELS:])


def scoped_structural_route_nodes(
    source_chain: list[OrgNode],
    target_chain: list[OrgNode],
    route_start_node_id: int,
) -> tuple[list[tuple[OrgNode, str]], str | None]:
    """Build source ascent and target descent without traversing excluded ancestors."""
    target_start_index = next(
        (
            index
            for index, node in enumerate(target_chain)
            if int(node.id) == int(route_start_node_id)
        ),
        None,
    )
    if target_start_index is None or target_start_index < DYNAMIC_ROUTE_EXCLUDED_TOP_LEVELS:
        return [], "نقطة بدء التسلسل لا تنتمي إلى الجزء المسموح من مسار الجهة المختارة."

    source_start_index = next(
        (
            index
            for index, node in enumerate(source_chain)
            if int(node.id) == int(route_start_node_id)
        ),
        None,
    )
    if source_start_index is None:
        source_start_index = target_start_index
    if source_start_index >= len(source_chain):
        return [], "تعذر تحديد مستوى أفقي موازٍ لنقطة بدء التسلسل ضمن هيكل منشئ الطلب."

    route: list[tuple[OrgNode, str]] = []
    for node in reversed(source_chain[source_start_index:]):
        route.append((node, "SOURCE_ASCENT"))
    for node in target_chain[target_start_index:]:
        if route and int(route[-1][0].id) == int(node.id):
            continue
        route.append((node, "TARGET_DESCENT"))
    return route, None


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


def dynamic_user_choices(requester: User) -> list[dict]:
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
        if not chain:
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
            "node_id": int(node_id),
            "node_label": node_path_label(node_id),
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
        chain = node_chain(node_id)
        route_start_options = []
        for chain_index, start_node in enumerate(chain):
            if chain_index < DYNAMIC_ROUTE_EXCLUDED_TOP_LEVELS:
                continue
            start_manager, start_manager_role = manager_context.get(
                int(start_node.id),
                (None, None),
            )
            is_secretary_general = _is_secretary_general_node(start_node)
            can_start = bool(
                start_manager
                and _node_allows_approval(start_node)
                and not is_secretary_general
            )
            route_start_options.append({
                "id": int(start_node.id),
                "position": chain_index + 2,
                "name": start_node.name_ar,
                "type_name": _node_type_name(start_node) or _node_type_code(start_node),
                "can_start": can_start,
                "is_secretary_general": is_secretary_general,
                "unavailable_reason": (
                    "يُضاف الأمين العام فقط من خيار الإضافة المستقل في نهاية المسار."
                    if is_secretary_general else
                    "لا يوجد مسؤول معتمد لهذا المستوى."
                    if not can_start else
                    ""
                ),
                "manager_name": (
                    start_manager.full_name or start_manager.email or f"مستخدم #{start_manager.id}"
                    if start_manager else ""
                ),
                "manager_role": start_manager_role or "",
            })
        has_route_start = any(option["can_start"] for option in route_start_options)
        has_manager = bool(manager and _node_allows_approval(node))
        result.append({
            "id": node_id,
            "parent_id": int(node.parent_id) if node.parent_id in nodes_by_id else None,
            "name": node.name_ar,
            "type_name": _node_type_name(node) or _node_type_code(node),
            "node_label": node_path_label(node),
            "direct_user_count": direct_counts.get(node_id, 0),
            "total_user_count": employee_count,
            "can_select": bool(has_manager and has_route_start),
            "has_manager": has_manager,
            "route_start_options": route_start_options,
            "unavailable_reason": (
                "هذه الجهة ضمن أول مستويين المستبعدين من المسار الديناميكي."
                if not route_start_options else
                "لا توجد نقطة بدء متاحة بمسؤول معتمد ضمن مسار هذه الجهة."
                if not has_route_start else
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

    def add_user_step(user: User, reason: str, node: OrgNode | None = None) -> bool:
        if int(user.id) == int(requester.id) and not steps:
            return False
        if steps and int(steps[-1]["approver_user_id"]) == int(user.id):
            return False
        steps.append({
            "step_order": len(steps) + 1,
            "mode": "SEQUENTIAL",
            "approver_kind": "USER",
            "approver_user_id": int(user.id),
            "sla_days": None,
            "label": user.full_name or user.email or f"مستخدم #{user.id}",
            "job_title": (getattr(user, "job_title", None) or "").strip(),
            "reason": reason,
            "node_id": int(node.id) if node else None,
            "node_label": node_path_label(node) if node else "",
        })
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

        if direct:
            add_user_step(
                target_user,
                "اختيار مباشر ضمن الإدارة نفسها (أفقي أو عمودي)",
                target_chain[-1],
            )
        else:
            route_nodes = structural_route_nodes(int(current_chain[-1].id), int(target_chain[-1].id))
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
                    f"{manager_role} «{node.name_ar}» ضمن التسلسل الإداري",
                    node,
                ):
                    segment["intermediate_manager_count"] += 1

            if not resolved_manager_count:
                errors.append(
                    f"لا يمكن الانتقال من «{current_user.full_name}» إلى «{target_user.full_name}»: "
                    "لم يتم تعيين مسؤول أو نائب مسؤول على التسلسل الإداري بينهما."
                )
            elif skipped_nodes:
                warnings.append(
                    "تم تجاوز عناصر بلا مسؤول معيّن بين "
                    f"«{current_user.full_name}» و«{target_user.full_name}»: "
                    + "، ".join(skipped_nodes[:6])
                )

            add_user_step(
                target_user,
                "المستلم المختار بعد المرور بالتسلسل الإداري",
                target_chain[-1],
            )

        segments.append(segment)
        current_user = target_user
        current_chain = target_chain

    for index, step in enumerate(steps, start=1):
        step["step_order"] = index

    return {
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
) -> dict:
    """Expand ordered USER/NODE/COMMITTEE targets into sequential runtime steps."""
    target_refs, errors = _normalized_dynamic_target_refs(selected_target_refs)
    if not target_refs:
        errors.append("اختر جهة تنظيمية أو شخصاً أو لجنة واحدة على الأقل للمسار الديناميكي.")

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
    requires_org_chain = bool(
        include_secretary_general
        or any(kind != "COMMITTEE" for kind, _target_id, _start_id, _mode in target_refs)
    )
    if not requester_chain and requires_org_chain:
        errors.append("يجب ربط منشئ الطلب بعنصر أساسي في الهيكل التنظيمي أولاً.")

    resolved_targets: list[dict] = []
    for kind, target_id, route_start_id, committee_delivery_mode in target_refs:
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
            if not target_chain:
                errors.append(f"المستخدم «{target_user.full_name}» غير مربوط بالهيكل التنظيمي.")
                continue
            resolved_targets.append({
                "kind": "USER",
                "id": target_id,
                "user": target_user,
                "node": target_chain[-1],
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
        route_start_node = None
        if route_start_id is not None:
            allowed_start_nodes = {
                int(start_node.id): start_node
                for start_node in dynamic_route_start_nodes(target_chain)
            }
            route_start_node = allowed_start_nodes.get(int(route_start_id))
            if not route_start_node:
                errors.append(
                    f"نقطة بدء التسلسل المحددة لا تقع ضمن الجزء المسموح من مسار الجهة «{target_node.name_ar}»."
                )
                continue
            if _is_secretary_general_node(route_start_node):
                errors.append(
                    "لا يمكن استخدام الأمين العام كنقطة بدء ضمنية؛ "
                    "أضفه من خيار «هل تريد إضافة الأمين العام كآخر خطوة؟»."
                )
                continue
            route_start_manager, _route_start_role = _manager_for_node(route_start_node)
            if not route_start_manager or not _node_allows_approval(route_start_node):
                errors.append(
                    f"لا يمكن بدء التسلسل من «{route_start_node.name_ar}» لعدم وجود مسؤول معتمد عليها."
                )
                continue
        resolved_targets.append({
            "kind": "NODE",
            "id": target_id,
            "user": target_manager,
            "node": target_node,
            "chain": target_chain,
            "manager_role": manager_role or "مسؤول",
            "route_start_node": route_start_node,
            "route_start_node_id": int(route_start_node.id) if route_start_node else None,
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

    def add_target_step(target: dict, direct: bool) -> bool:
        target_user = target["user"]
        target_node = target["node"]
        if target["kind"] == "USER":
            return add_user_step(
                target_user,
                "اختيار مباشر ضمن الإدارة نفسها (أفقي أو عمودي)"
                if direct else
                "المستلم المختار بعد المرور بالتسلسل الإداري",
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
            "label": target["label"],
            "job_title": (getattr(target_user, "job_title", None) or "").strip(),
            "reason": "",
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

    current_user = requester
    current_chain = requester_chain
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
        direct = same_administration(current_chain, target_chain)
        route_start_node = target.get("route_start_node")
        target_ref = f"{target['kind']}:{int(target['id'])}"
        if route_start_node:
            target_ref += f"@{int(route_start_node.id)}"
        segment = {
            "from_user_id": int(current_user.id),
            "to_user_id": int(target_user.id),
            "target_kind": target["kind"],
            "target_id": int(target["id"]),
            "target_ref": target_ref,
            "route_start_node_id": int(route_start_node.id) if route_start_node else None,
            "route_start_label": node_path_label(route_start_node) if route_start_node else "",
            "same_administration": direct,
            "intermediate_manager_count": 0,
        }

        if route_start_node:
            scoped_route, route_error = scoped_structural_route_nodes(
                current_chain,
                target_chain,
                int(route_start_node.id),
            )
            if route_error:
                errors.append(
                    f"لا يمكن بناء المسار من «{current_user.full_name}» إلى «{target['label']}»: {route_error}"
                )
                segments.append(segment)
                continue

            skipped_nodes = []
            for route_node, _route_phase in scoped_route:
                if _is_secretary_general_node(route_node):
                    continue
                if int(route_node.id) == int(target_node.id):
                    continue
                if not _node_allows_approval(route_node):
                    continue
                manager, _manager_role = _manager_for_node(route_node)
                if not manager:
                    skipped_nodes.append(route_node.name_ar)
                    continue
                if add_user_step(manager, "", route_node):
                    segment["intermediate_manager_count"] += 1

            if skipped_nodes:
                warnings.append(
                    f"تم تجاوز عناصر بلا مسؤول معيّن ضمن التسلسل المحدد من «{route_start_node.name_ar}»: "
                    + "، ".join(skipped_nodes[:8])
                )
            add_target_step(target, False)
        elif direct:
            add_target_step(target, True)
        else:
            route_nodes = structural_route_nodes(int(current_chain[-1].id), int(target_chain[-1].id))
            resolved_manager_count = 0
            skipped_nodes = []
            for route_node in route_nodes:
                if _is_secretary_general_node(route_node):
                    continue
                if target["kind"] == "NODE" and int(route_node.id) == int(target_node.id):
                    continue
                if not _node_allows_approval(route_node):
                    continue
                manager, manager_role = _manager_for_node(route_node)
                if not manager:
                    skipped_nodes.append(route_node.name_ar)
                    continue
                resolved_manager_count += 1
                if add_user_step(
                    manager,
                    f"{manager_role} «{route_node.name_ar}» ضمن التسلسل الإداري",
                    route_node,
                ):
                    segment["intermediate_manager_count"] += 1

            if not resolved_manager_count:
                errors.append(
                    f"لا يمكن الانتقال من «{current_user.full_name}» إلى «{target['label']}»: "
                    "لم يتم تعيين مسؤول أو نائب مسؤول على التسلسل الإداري بينهما."
                )
            elif skipped_nodes:
                warnings.append(
                    "تم تجاوز عناصر بلا مسؤول معيّن بين "
                    f"«{current_user.full_name}» و«{target['label']}»: "
                    + "، ".join(skipped_nodes[:6])
                )
            add_target_step(target, False)

        segments.append(segment)
        current_user = target_user
        current_chain = target_chain

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
                "label": f"{_node_type_name(secretary_general) or _node_type_code(secretary_general)}: {secretary_general.name_ar}",
                "job_title": (getattr(secretary_manager, "job_title", None) or "").strip(),
                "reason": "",
                "node_id": int(secretary_general.id),
                "node_label": node_path_label(secretary_general),
            })

    # A dynamic route is a round trip.  After the final destination acts, the
    # request returns through the same approvals in reverse order.  The final
    # destination itself is not duplicated; completing the last return step
    # hands the request back to its creator through the normal completion flow.
    forward_steps = list(steps)
    for forward_step in reversed(forward_steps[:-1]):
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
        "steps": steps,
        "segments": segments,
        "warnings": warnings,
        "errors": list(dict.fromkeys(errors)),
        "include_secretary_general": bool(include_secretary_general),
    }
