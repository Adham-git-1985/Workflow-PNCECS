from __future__ import annotations

from datetime import datetime
import json

from sqlalchemy import inspect, text as sa_text
from sqlalchemy.orm import selectinload

from extensions import db
from models import (
    SystemSetting,
    OrgNodeType, OrgNode, OrgNodeManager, OrgNodeAssignment,
    Organization, Directorate, Unit, Department, Section, Division, Team, TeamMembership, User,
    OrgUnitManager, OrgUnitAssignment,
)


def _get_setting(key: str) -> str | None:
    row = SystemSetting.query.filter_by(key=key).first()
    return (row.value if row else None)


def _set_setting(key: str, value: str):
    row = SystemSetting.query.filter_by(key=key).first()
    if not row:
        row = SystemSetting(key=key, value=value, created_at=datetime.utcnow())
        db.session.add(row)
    else:
        row.value = value


DEFAULT_TYPES = [
    # (code, name_ar, name_en, sort, allowed_parent_codes)
    ("ORGANIZATION", "منظمة", "Organization", 10, []),
    ("DIRECTORATE", "إدارة", "Directorate", 20, ["ORGANIZATION"]),
    ("UNIT", "وحدة", "Unit", 30, ["ORGANIZATION"]),
    ("DEPARTMENT", "دائرة", "Department", 40, ["DIRECTORATE", "UNIT"]),
    ("SECTION", "قسم", "Section", 50, ["DEPARTMENT", "UNIT", "DIRECTORATE"]),
    ("DIVISION", "شعبة", "Division", 60, ["SECTION"]),
    ("TEAM", "فريق", "Team", 70, ["SECTION", "DIVISION"]),
]



def _ensure_org_nodes_sort_order_column():
    """Ensure org_nodes.sort_order exists to keep ordering stable.

    We add the column lazily for existing databases (ALTER TABLE).
    Safe to call multiple times.
    """
    try:
        insp = inspect(db.engine)
        cols = [c.get('name') for c in insp.get_columns('org_nodes')]
        if 'sort_order' in cols:
            return
        # Add column (SQLite/MySQL/PostgreSQL compatible enough)
        db.session.execute(sa_text('ALTER TABLE org_nodes ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0'))
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        return


def ensure_dynamic_org_seed():
    """Create default dynamic org types + sync existing legacy org data into OrgNodes once.

    Safe to call multiple times.
    """
    try:
        # Ensure tables exist
        OrgNodeType.query.limit(1).all()
    except Exception as e:
        msg = str(e).lower()
        if "no such table" in msg or "doesn't exist" in msg:
            try:
                db.create_all()
                OrgNodeType.query.limit(1).all()
            except Exception:
                return
        else:
            return

    _ensure_org_nodes_sort_order_column()

    try:
        if OrgNodeType.query.count() == 0:
            # Create default types
            type_by_code: dict[str, OrgNodeType] = {}
            for code, ar, en, order, _ in DEFAULT_TYPES:
                t = OrgNodeType(
                    code=code,
                    name_ar=ar,
                    name_en=en,
                    sort_order=order,
                    allow_in_approvals=True,
                    show_in_chart=True,
                    show_in_routes=True,
                    is_active=True,
                    created_at=datetime.utcnow(),
                )
                db.session.add(t)
                type_by_code[code] = t
            db.session.flush()

            # Set allowed parents as IDs
            for code, _, _, _, parent_codes in DEFAULT_TYPES:
                t = type_by_code.get(code)
                if not t:
                    continue
                parent_ids = [type_by_code[p].id for p in parent_codes if p in type_by_code]
                t.set_allowed_parent_type_ids(parent_ids)

            db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


def sync_legacy_now(*, raise_errors: bool = False) -> bool:
    """Force-sync legacy org tables/managers/assignments into dynamic OrgNodes.

    This does NOT use a one-time migration guard and is useful after CRUD
    operations on legacy elements. It becomes a no-op while the legacy
    structure is locked or an approved canonical version exists, so it cannot
    reintroduce old nodes into the official chart.
    """
    try:
        legacy_locked = (_get_setting("ORG_LEGACY_LOCKED") or "").strip() == "1"
        approved_version = (_get_setting("ORG_APPROVED_STRUCTURE_VERSION") or "").strip()
        if legacy_locked or approved_version:
            return False
    except Exception:
        pass

    try:
        # Ensure dynamic schema/types exist before syncing
        ensure_dynamic_org_seed()

        _sync_legacy_nodes()
        _sync_legacy_managers()
        _sync_legacy_assignments()
        _sync_legacy_team_memberships()
        db.session.commit()
        return True
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        if raise_errors:
            raise
        return False


def sync_existing_legacy_node(legacy_type: str, legacy_id: int) -> bool:
    """Refresh one existing legacy-backed OrgNode in the current transaction.

    Unlike ``sync_legacy_now``, this helper remains safe when the approved
    structure is enabled: it never creates legacy nodes and never touches an
    approved canonical node.  It only keeps an already-visible legacy mirror
    aligned after its source master-data row is edited.
    """
    legacy_type = (legacy_type or "").strip().upper()
    try:
        legacy_id = int(legacy_id)
    except (TypeError, ValueError):
        return False

    model_by_type = {
        "ORGANIZATION": Organization,
        "DIRECTORATE": Directorate,
        "UNIT": Unit,
        "DEPARTMENT": Department,
        "SECTION": Section,
        "DIVISION": Division,
        "TEAM": Team,
    }
    model = model_by_type.get(legacy_type)
    if model is None:
        return False

    source = db.session.get(model, legacy_id)
    node = (
        OrgNode.query
        .filter_by(
            legacy_type=legacy_type,
            legacy_id=legacy_id,
            is_active=True,
        )
        .order_by(OrgNode.id.asc())
        .first()
    )
    if source is None or node is None:
        return False

    parent_identity: tuple[str, int] | None = None
    if legacy_type in {"DIRECTORATE", "UNIT"}:
        parent_id = getattr(source, "organization_id", None)
        if parent_id:
            parent_identity = ("ORGANIZATION", int(parent_id))
    elif legacy_type == "DEPARTMENT":
        if getattr(source, "unit_id", None):
            parent_identity = ("UNIT", int(source.unit_id))
        elif getattr(source, "directorate_id", None):
            parent_identity = ("DIRECTORATE", int(source.directorate_id))
    elif legacy_type == "SECTION":
        if getattr(source, "department_id", None):
            parent_identity = ("DEPARTMENT", int(source.department_id))
        elif getattr(source, "unit_id", None):
            parent_identity = ("UNIT", int(source.unit_id))
        elif getattr(source, "directorate_id", None):
            parent_identity = ("DIRECTORATE", int(source.directorate_id))
    elif legacy_type == "DIVISION":
        if getattr(source, "section_id", None):
            parent_identity = ("SECTION", int(source.section_id))
        elif getattr(source, "department_id", None):
            parent_identity = ("DEPARTMENT", int(source.department_id))
    elif legacy_type == "TEAM":
        if getattr(source, "division_id", None):
            parent_identity = ("DIVISION", int(source.division_id))
        elif getattr(source, "section_id", None):
            parent_identity = ("SECTION", int(source.section_id))

    parent_node = None
    if parent_identity:
        parent_node = (
            OrgNode.query
            .filter_by(
                legacy_type=parent_identity[0],
                legacy_id=parent_identity[1],
                is_active=True,
            )
            .order_by(OrgNode.id.asc())
            .first()
        )
        if parent_node is None:
            return False
    elif legacy_type != "ORGANIZATION":
        return False

    node.parent_id = parent_node.id if parent_node else None
    node.name_ar = getattr(source, "name_ar", None) or node.name_ar
    node.name_en = getattr(source, "name_en", None) or node.name_en
    node.code = getattr(source, "code", None) or node.code
    node.is_active = bool(getattr(source, "is_active", True))
    node.updated_at = datetime.utcnow()
    db.session.flush()
    return True


def get_node_ancestor_ids(node_id: int) -> set[int]:
    """Return {node_id, parent_id, ...} up to root."""
    ids: set[int] = set()
    cur = int(node_id) if node_id else None
    guard = 0
    while cur and guard < 200:
        if cur in ids:
            break
        ids.add(cur)
        parent = db.session.query(OrgNode.parent_id).filter(OrgNode.id == cur).scalar()
        cur = parent
        guard += 1
    return ids


def resolve_user_org_node_id(user) -> int | None:
    """Best-effort resolve a user's effective OrgNode.

    Priority:
      1) OrgNodeAssignment primary
      2) User.org_node_id
      3) Legacy mapping via user's (division/section/unit/department/directorate)
    """
    try:
        a = (
            OrgNodeAssignment.query
            .join(OrgNode, OrgNodeAssignment.node_id == OrgNode.id)
            .filter(
                OrgNodeAssignment.user_id == user.id,
                OrgNodeAssignment.is_primary.is_(True),
                OrgNode.is_active.is_(True),
            )
            .order_by(OrgNodeAssignment.id.desc())
            .first()
        )
        if a:
            return int(a.node_id)
    except Exception:
        pass

    try:
        if getattr(user, "org_node_id", None):
            node = db.session.get(OrgNode, int(user.org_node_id))
            if node and node.is_active:
                return int(node.id)
    except Exception:
        pass

    # Legacy fallback (best available)
    for legacy_field, legacy_type in (
        ("division_id", "DIVISION"),
        ("section_id", "SECTION"),
        ("unit_id", "UNIT"),
        ("department_id", "DEPARTMENT"),
        ("directorate_id", "DIRECTORATE"),
    ):
        try:
            val = getattr(user, legacy_field, None)
            if val:
                n = get_node_by_legacy(legacy_type, int(val))
                if n:
                    return n.id
        except Exception:
            continue

    return None


def build_chart_tree(include_people: bool = False) -> list[dict]:
    """Build nested dict tree for UI/exports.

    Respects OrgNodeType.show_in_chart by *lifting* children of hidden types.
    """
    ensure_dynamic_org_seed()

    types = OrgNodeType.query.filter_by(is_active=True).all()
    type_by_id = {t.id: t for t in types}

    nodes = (
        OrgNode.query
        .filter(OrgNode.is_active == True)
        .order_by(OrgNode.parent_id.asc().nullslast(), OrgNode.type_id.asc(), OrgNode.sort_order.asc().nullslast(), OrgNode.name_ar.asc())
        .all()
    )

    node_by_id = {n.id: n for n in nodes}
    children_map: dict[int | None, list[int]] = {}
    for n in nodes:
        children_map.setdefault(n.parent_id, []).append(n.id)

    mgr_rows = OrgNodeManager.query.all()
    mgr_map = {m.node_id: m for m in mgr_rows}

    people_map: dict[int, list[dict]] = {}
    if include_people:
        assigns = OrgNodeAssignment.query.order_by(OrgNodeAssignment.is_primary.desc(), OrgNodeAssignment.id.asc()).all()
        for a in assigns:
            if not a.user:
                continue
            people_map.setdefault(a.node_id, []).append({
                "name": a.user.full_name,
                "title": (a.title or "").strip() or None,
                "is_primary": bool(a.is_primary),
            })

    needed_user_ids: set[int] = set()
    for m in mgr_rows:
        if m.manager_user_id:
            needed_user_ids.add(int(m.manager_user_id))
        if m.deputy_user_id:
            needed_user_ids.add(int(m.deputy_user_id))
    users_map = {}
    if needed_user_ids:
        try:
            for u in User.query.filter(User.id.in_(list(needed_user_ids))).all():
                users_map[u.id] = u
        except Exception:
            users_map = {}

    def _mgr_name(uid: int | None) -> str | None:
        if not uid:
            return None
        u = users_map.get(int(uid))
        return u.full_name if u else None

    def _build(node_id: int):
        n = node_by_id.get(node_id)
        if not n:
            return []
        t = type_by_id.get(n.type_id)
        visible = bool(t and t.show_in_chart)

        kids_out: list[dict] = []
        for cid in children_map.get(node_id, []):
            built = _build(cid)
            if isinstance(built, list):
                kids_out.extend(built)
            else:
                kids_out.append(built)

        if not visible:
            return kids_out

        mgr = mgr_map.get(node_id)
        return {
            "id": n.id,
            "type": (t.code if t else "NODE"),
            "type_name": (t.name_ar if t else "عنصر"),
            "name_ar": n.name_ar,
            "name_en": n.name_en,
            "code": n.code,
            "manager": _mgr_name(getattr(mgr, "manager_user_id", None)),
            "deputy": _mgr_name(getattr(mgr, "deputy_user_id", None)),
            "members": people_map.get(node_id, []) if include_people else [],
            "children": kids_out,
        }

    roots: list[int] = []
    for n in nodes:
        if not n.parent_id or n.parent_id not in node_by_id:
            roots.append(n.id)

    out: list[dict] = []
    for rid in roots:
        built = _build(rid)
        if isinstance(built, list):
            out.extend(built)
        else:
            out.append(built)

    return out


def build_org_node_picker_tree(mode: str = "all") -> list[dict]:
    """Build a nested tree for *picking* an OrgNode in UI.

    Unlike build_chart_tree(), this does not "lift" hidden types; it returns the
    real hierarchy and sets an `eligible` flag per node based on mode:

      - approvals: OrgNodeType.allow_in_approvals
      - routes:    OrgNodeType.show_in_routes
      - chart:     OrgNodeType.show_in_chart
      - all:       all active nodes eligible
    """
    ensure_dynamic_org_seed()

    m = (mode or "all").strip().lower()

    nodes = (
        OrgNode.query
        .options(selectinload(OrgNode.type))
        .filter(OrgNode.is_active == True)
        .order_by(
            OrgNode.parent_id.asc().nullslast(),
            OrgNode.type_id.asc(),
            OrgNode.sort_order.asc().nullslast(),
            OrgNode.name_ar.asc(),
        )
        .all()
    )

    children_map: dict[int | None, list[OrgNode]] = {}
    for n in nodes:
        children_map.setdefault(n.parent_id, []).append(n)

    def _eligible(n: OrgNode) -> bool:
        t = getattr(n, "type", None)
        if m == "approvals":
            return bool(t and getattr(t, "allow_in_approvals", False))
        if m == "routes":
            return bool(t and getattr(t, "show_in_routes", False))
        if m == "chart":
            return bool(t and getattr(t, "show_in_chart", False))
        return True

    def to_dict(n: OrgNode) -> dict | None:
        t = getattr(n, "type", None)
        type_name = (t.name_ar if t else "")
        type_code = (t.code if t else "")
        label = f"{(type_name or type_code).strip()} — {n.name_ar}".strip(" —")
        children = [to_dict(ch) for ch in children_map.get(n.id, [])]
        children = [c for c in children if c is not None]

        eligible = bool(_eligible(n))
        # prune nodes that are neither eligible nor have eligible descendants
        if m != "all" and (not eligible) and (not children):
            return None

        return {
            "id": n.id,
            "label": label,
            "name_ar": n.name_ar,
            "code": n.code,
            "type_name": type_name,
            "type_code": type_code,
            "eligible": eligible,
            "children": children,
        }

    roots = [to_dict(n) for n in children_map.get(None, [])]
    return [r for r in roots if r is not None]


def _type_id_by_code() -> dict[str, int]:
    rows = OrgNodeType.query.all()
    return { (r.code or "").strip().upper(): r.id for r in rows if (r.code or "").strip() }


def _get_or_create_node(type_code: str, legacy_type: str | None, legacy_id: int | None,
                        name_ar: str, name_en: str | None, code: str | None,
                        parent_id: int | None) -> OrgNode:
    type_code_u = (type_code or "").strip().upper()
    tmap = _type_id_by_code()
    t_id = tmap.get(type_code_u)
    if not t_id:
        # Fallback: create the type
        t = OrgNodeType(code=type_code_u, name_ar=type_code_u, name_en=type_code_u, sort_order=999)
        db.session.add(t)
        db.session.flush()
        t_id = t.id

    q = OrgNode.query
    if legacy_type and legacy_id is not None:
        node = q.filter_by(legacy_type=legacy_type, legacy_id=int(legacy_id)).first()
        if node:
            # keep names fresh
            node.name_ar = name_ar or node.name_ar
            node.name_en = name_en or node.name_en
            node.code = code or node.code
            node.type_id = t_id
            node.parent_id = parent_id
            return node

    # Non-legacy node: create always
    node = OrgNode(
        type_id=t_id,
        parent_id=parent_id,
        name_ar=name_ar,
        name_en=name_en,
        code=code,
        is_active=True,
        legacy_type=legacy_type,
        legacy_id=int(legacy_id) if legacy_id is not None else None,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.session.add(node)
    db.session.flush()
    return node


def _sync_legacy_nodes():
    # Organizations
    org_nodes: dict[int, int] = {}
    for o in Organization.query.order_by(Organization.id.asc()).all():
        n = _get_or_create_node(
            "ORGANIZATION",
            "ORGANIZATION",
            o.id,
            o.name_ar,
            getattr(o, "name_en", None),
            getattr(o, "code", None),
            None,
        )
        n.is_active = bool(getattr(o, "is_active", True))
        org_nodes[o.id] = n.id

    # Directorates
    dir_nodes: dict[int, int] = {}
    for d in Directorate.query.order_by(Directorate.id.asc()).all():
        parent = org_nodes.get(d.organization_id)
        n = _get_or_create_node(
            "DIRECTORATE",
            "DIRECTORATE",
            d.id,
            d.name_ar,
            getattr(d, "name_en", None),
            getattr(d, "code", None),
            parent,
        )
        n.is_active = bool(getattr(d, "is_active", True))
        dir_nodes[d.id] = n.id

    # Units
    unit_nodes: dict[int, int] = {}
    for u in Unit.query.order_by(Unit.id.asc()).all():
        parent = org_nodes.get(getattr(u, "organization_id", None))
        n = _get_or_create_node(
            "UNIT",
            "UNIT",
            u.id,
            u.name_ar,
            getattr(u, "name_en", None),
            getattr(u, "code", None),
            parent,
        )
        n.is_active = bool(getattr(u, "is_active", True))
        unit_nodes[u.id] = n.id

    # Departments (may belong to directorate or unit)
    dept_nodes: dict[int, int] = {}
    for dp in Department.query.order_by(Department.id.asc()).all():
        parent = None
        if getattr(dp, "unit_id", None):
            parent = unit_nodes.get(dp.unit_id)
        if parent is None:
            parent = dir_nodes.get(dp.directorate_id)
        n = _get_or_create_node(
            "DEPARTMENT",
            "DEPARTMENT",
            dp.id,
            dp.name_ar,
            getattr(dp, "name_en", None),
            getattr(dp, "code", None),
            parent,
        )
        n.is_active = bool(getattr(dp, "is_active", True))
        dept_nodes[dp.id] = n.id

    # Sections (may belong to department, unit or directorate)
    sec_nodes: dict[int, int] = {}
    for s in Section.query.order_by(Section.id.asc()).all():
        parent = None
        if getattr(s, "department_id", None):
            parent = dept_nodes.get(s.department_id)
        if parent is None and getattr(s, "unit_id", None):
            parent = unit_nodes.get(s.unit_id)
        if parent is None:
            parent = dir_nodes.get(s.directorate_id)
        n = _get_or_create_node(
            "SECTION",
            "SECTION",
            s.id,
            s.name_ar,
            getattr(s, "name_en", None),
            getattr(s, "code", None),
            parent,
        )
        n.is_active = bool(getattr(s, "is_active", True))
        sec_nodes[s.id] = n.id

    # Divisions under Section
    div_nodes: dict[int, int] = {}
    for dv in Division.query.order_by(Division.id.asc()).all():
        parent = sec_nodes.get(getattr(dv, "section_id", None))
        if parent is None:
            parent = dept_nodes.get(getattr(dv, "department_id", None))
        n = _get_or_create_node(
            "DIVISION",
            "DIVISION",
            dv.id,
            dv.name_ar,
            getattr(dv, "name_en", None),
            getattr(dv, "code", None),
            parent,
        )
        n.is_active = bool(getattr(dv, "is_active", True))
        div_nodes[dv.id] = n.id

    # Teams under Section or Division
    try:
        team_rows = Team.query.order_by(Team.id.asc()).all()
    except Exception:
        team_rows = []
    active_legacy_team_ids = set()
    for tm in team_rows:
        active_legacy_team_ids.add(int(tm.id))
        parent = None
        if getattr(tm, "division_id", None):
            parent = div_nodes.get(tm.division_id)
        if parent is None:
            parent = sec_nodes.get(tm.section_id)
        node = _get_or_create_node(
            "TEAM",
            "TEAM",
            tm.id,
            tm.name_ar,
            getattr(tm, "name_en", None),
            getattr(tm, "code", None),
            parent,
        )
        node.is_active = bool(getattr(tm, "is_active", True))

    for stale_node in OrgNode.query.filter_by(legacy_type="TEAM").all():
        if int(stale_node.legacy_id or 0) not in active_legacy_team_ids:
            stale_node.is_active = False

    db.session.flush()


def _sync_legacy_managers():
    # copy OrgUnitManager -> OrgNodeManager where possible
    try:
        mgr_rows = OrgUnitManager.query.all()
    except Exception:
        mgr_rows = []

    for m in mgr_rows:
        ut = (m.unit_type or "").strip().upper()
        uid = getattr(m, "unit_id", None)
        if uid is None:
            continue
        node = OrgNode.query.filter_by(legacy_type=ut, legacy_id=int(uid)).first()
        if not node:
            continue
        row = OrgNodeManager.query.filter_by(node_id=node.id).first()
        if not row:
            row = OrgNodeManager(node_id=node.id, updated_at=datetime.utcnow(), updated_by_id=getattr(m, "updated_by_id", None))
            db.session.add(row)
        row.manager_user_id = getattr(m, "manager_user_id", None)
        row.deputy_user_id = getattr(m, "deputy_user_id", None)

    db.session.flush()


def _sync_legacy_assignments():
    # copy OrgUnitAssignment -> OrgNodeAssignment where possible
    try:
        rows = OrgUnitAssignment.query.all()
    except Exception:
        rows = []

    legacy_primary_nodes: dict[int, int] = {}
    for a in rows:
        ut = (a.unit_type or "").strip().upper()
        if ut == "TEAM":
            continue
        uid = getattr(a, "unit_id", None)
        if uid is None:
            continue
        node = OrgNode.query.filter_by(legacy_type=ut, legacy_id=int(uid)).first()
        if not node:
            continue
        row = OrgNodeAssignment.query.filter_by(user_id=a.user_id, node_id=node.id).first()
        if not row:
            row = OrgNodeAssignment(
                user_id=a.user_id,
                node_id=node.id,
                created_at=getattr(a, "created_at", None) or datetime.utcnow(),
                created_by_id=getattr(a, "created_by_id", None),
            )
            db.session.add(row)
        row.title = getattr(a, "title", None)
        row.is_primary = bool(getattr(a, "is_primary", False))
        if row.is_primary:
            legacy_primary_nodes[int(a.user_id)] = int(node.id)

    for user_id, node_id in legacy_primary_nodes.items():
        (
            OrgNodeAssignment.query
            .filter(
                OrgNodeAssignment.user_id == user_id,
                OrgNodeAssignment.node_id != node_id,
            )
            .update({OrgNodeAssignment.is_primary: False}, synchronize_session=False)
        )
        user = db.session.get(User, user_id)
        if user:
            user.org_node_id = node_id

    # Ensure single primary per user in node assignments as well
    try:
        from sqlalchemy import func
        user_ids = [r[0] for r in db.session.query(OrgNodeAssignment.user_id).distinct().all()]
        for uid in user_ids:
            prim = OrgNodeAssignment.query.filter_by(user_id=uid, is_primary=True).first()
            if not prim:
                first = OrgNodeAssignment.query.filter_by(user_id=uid).order_by(OrgNodeAssignment.id.asc()).first()
                if first:
                    first.is_primary = True
    except Exception:
        pass

    db.session.flush()


def _sync_legacy_team_memberships():
    """Copy old TEAM unit assignments into independent memberships."""
    try:
        rows = OrgUnitAssignment.query.filter(
            db.func.upper(OrgUnitAssignment.unit_type) == "TEAM"
        ).all()
    except Exception:
        rows = []

    affected_user_ids = set()
    for assignment in rows:
        affected_user_ids.add(int(assignment.user_id))
        assignment.is_primary = False
        team = db.session.get(Team, int(assignment.unit_id))
        if not team:
            continue
        membership = TeamMembership.query.filter_by(
            team_id=team.id,
            user_id=assignment.user_id,
        ).first()
        if not membership:
            membership = TeamMembership(
                team_id=team.id,
                user_id=assignment.user_id,
                created_at=getattr(assignment, "created_at", None) or datetime.utcnow(),
                created_by_id=getattr(assignment, "created_by_id", None),
            )
            db.session.add(membership)
        membership.title = getattr(assignment, "title", None)
        membership.is_active = True

        team_node = OrgNode.query.filter_by(legacy_type="TEAM", legacy_id=team.id).first()
        if team_node:
            old_org_assignment = OrgNodeAssignment.query.filter_by(
                user_id=assignment.user_id,
                node_id=team_node.id,
            ).first()
            if old_org_assignment:
                db.session.delete(old_org_assignment)
            user = db.session.get(User, int(assignment.user_id))
            if user and int(getattr(user, "org_node_id", 0) or 0) == int(team_node.id):
                user.org_node_id = None

    for user_id in affected_user_ids:
        primary_org_assignment = (
            OrgUnitAssignment.query
            .filter(
                OrgUnitAssignment.user_id == user_id,
                db.func.upper(OrgUnitAssignment.unit_type) != "TEAM",
                OrgUnitAssignment.is_primary.is_(True),
            )
            .first()
        )
        if not primary_org_assignment:
            primary_org_assignment = (
                OrgUnitAssignment.query
                .filter(
                    OrgUnitAssignment.user_id == user_id,
                    db.func.upper(OrgUnitAssignment.unit_type) != "TEAM",
                )
                .order_by(OrgUnitAssignment.id.asc())
                .first()
            )
            if primary_org_assignment:
                primary_org_assignment.is_primary = True

    db.session.flush()
