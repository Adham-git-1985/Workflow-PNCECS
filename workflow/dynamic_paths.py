from __future__ import annotations

from extensions import db
from models import OrgNode, OrgNodeManager, Team, TeamMembership, User
from utils.org_dynamic import resolve_user_org_node_id


MAX_DYNAMIC_TARGETS = 20


def _node_type_code(node: OrgNode | None) -> str:
    node_type = getattr(node, "type", None)
    return (getattr(node_type, "code", "") or "").strip().upper()


def _node_type_name(node: OrgNode | None) -> str:
    node_type = getattr(node, "type", None)
    return (getattr(node_type, "name_ar", "") or getattr(node_type, "name_en", "") or "").strip()


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
    role = "مدير" if manager.manager_user_id else "نائب مدير"
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
        errors.append("لا يوجد مدير أو نائب مدير معيّن على عناصر هذا المسار الهيكلي.")
    warnings = []
    if skipped:
        warnings.append("تم تجاوز عناصر بلا مدير معيّن: " + "، ".join(skipped[:8]))
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
                    "لم يتم تعيين مدير أو نائب مدير على التسلسل الإداري بينهما."
                )
            elif skipped_nodes:
                warnings.append(
                    "تم تجاوز عناصر بلا مدير معيّن بين "
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
