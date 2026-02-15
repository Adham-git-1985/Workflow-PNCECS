from __future__ import annotations

from datetime import datetime
import json

from sqlalchemy import inspect, text as sa_text
from sqlalchemy.orm import selectinload

from extensions import db
from models import (
    SystemSetting,
    OrgNodeType, OrgNode, OrgNodeManager, OrgNodeAssignment,
    Organization, Directorate, Unit, Department, Section, Division, Team,
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
    ("UNIT", "وحدة", "Unit", 30, ["DIRECTORATE"]),
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


def sync_legacy_now():
    """Force-sync legacy org tables/managers/assignments into dynamic OrgNodes.

    This does NOT use a one-time SystemSetting guard.
    Useful after CRUD operations that create/update legacy org elements.
    """
    try:
        # Ensure dynamic schema/types exist before syncing
        ensure_dynamic_org_seed()

        _sync_legacy_nodes()
        _sync_legacy_managers()
        _sync_legacy_assignments()
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


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
      1) User.org_node_id
      2) OrgNodeAssignment primary
      3) Legacy mapping via user's (division/section/unit/department/directorate)
    """
    try:
        if getattr(user, "org_node_id", None):
            return int(user.org_node_id)
    except Exception:
        pass

    try:
        a = (
            OrgNodeAssignment.query
            .filter_by(user_id=user.id, is_primary=True)
            .order_by(OrgNodeAssignment.id.desc())
            .first()
        )
        if a:
            return int(a.node_id)
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
            "name_ar": n.name_ar,
            "name_en": n.name_en,
            "code": n.code,
            "manager": _mgr_name(getattr(mgr, "manager_user_id", None)),
            "deputy": _mgr_name(getattr(mgr, "deputy_user_id", None)),
            "members": people_map.get(node_id, []) if include_people else [],
            "children": kids_out,
        }


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

    # Sync legacy only once
    if (_get_setting("ORG_NODE_SYNC_LEGACY_V1") or "").strip() == "1":
        return

    try:
        _sync_legacy_nodes()
        _sync_legacy_managers()
        _sync_legacy_assignments()
        _set_setting("ORG_NODE_SYNC_LEGACY_V1", "1")
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


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
        dir_nodes[d.id] = n.id

    # Units
    unit_nodes: dict[int, int] = {}
    for u in Unit.query.order_by(Unit.id.asc()).all():
        parent = dir_nodes.get(u.directorate_id)
        n = _get_or_create_node(
            "UNIT",
            "UNIT",
            u.id,
            u.name_ar,
            getattr(u, "name_en", None),
            getattr(u, "code", None),
            parent,
        )
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
        sec_nodes[s.id] = n.id

    # Divisions under Section
    div_nodes: dict[int, int] = {}
    for dv in Division.query.order_by(Division.id.asc()).all():
        parent = sec_nodes.get(dv.section_id)
        n = _get_or_create_node(
            "DIVISION",
            "DIVISION",
            dv.id,
            dv.name_ar,
            getattr(dv, "name_en", None),
            getattr(dv, "code", None),
            parent,
        )
        div_nodes[dv.id] = n.id

    # Teams under Section or Division
    try:
        team_rows = Team.query.order_by(Team.id.asc()).all()
    except Exception:
        team_rows = []
    for tm in team_rows:
        parent = None
        if getattr(tm, "division_id", None):
            parent = div_nodes.get(tm.division_id)
        if parent is None:
            parent = sec_nodes.get(tm.section_id)
        _get_or_create_node(
            "TEAM",
            "TEAM",
            tm.id,
            tm.name_ar,
            getattr(tm, "name_en", None),
            getattr(tm, "code", None),
            parent,
        )

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

    for a in rows:
        ut = (a.unit_type or "").strip().upper()
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
