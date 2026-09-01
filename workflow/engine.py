# workflow/engine.py

from datetime import datetime, timedelta
import uuid

from sqlalchemy.exc import IntegrityError

from extensions import db
from utils.notification_links import notification_target_path
from utils.ui_labels import workflow_status_label
from models import (
    AuditLog,
    Notification,
    User,
    WorkflowRequest,
    WorkflowTemplate,
    WorkflowTemplateStep,
    WorkflowTemplateParallelAssignee,
    WorkflowInstance,
    WorkflowInstanceStep,
    WorkflowStepTask,
    Department,
    Directorate,
    CommitteeAssignee,
    OrgUnitManager,
    OrgNode,
    OrgNodeManager,
    SystemSetting,
)
from services.workflow_confidentiality import filter_confidential_workflow_user_ids


DYNAMIC_RETURN_REASON = "عودة المسار وفق التسلسل الإداري"
HIERARCHY_BYPASS_FOLLOWER_ACTION = "HIERARCHY_BYPASS_FOLLOWER"
# A mention task is intentionally allowed to add someone outside the original
# parallel-step candidate list. Keep the legacy Arabic marker so tasks created
# before this marker was standardized are not incorrectly bypassed on reload.
MENTION_TASK_MARKERS = frozenset({
    "MENTION_TASK",
    "تمت الإضافة عبر المنشن",
})


def is_mention_task(task: WorkflowStepTask | None) -> bool:
    """Return whether a runtime task was created by a workflow mention."""
    return bool(
        task
        and (
            (getattr(task, "note", None) or "").strip() in MENTION_TASK_MARKERS
            or (getattr(task, "note", None) or "").strip().startswith("MENTION_TASK")
        )
    )

# =========================
# SLA helpers
# =========================
def _system_sla_days():
    try:
        setting = SystemSetting.query.filter_by(key="SLA_DAYS").first()
        value = int(setting.value) if setting and setting.value is not None else 3
        return value if value > 0 else 3
    except (TypeError, ValueError):
        return 3


def _effective_sla_days(template_sla_days=None, step_sla_days=None):
    """Resolve and freeze a positive SLA duration for a runtime step."""
    for value in (step_sla_days, template_sla_days, _system_sla_days()):
        try:
            days = int(value)
        except (TypeError, ValueError):
            continue
        if days > 0:
            return days
    return 3


def _step_due_at(template_sla_days=None, step_sla_days=None, started_at=None):
    days = _effective_sla_days(template_sla_days, step_sla_days)
    return (started_at or datetime.utcnow()) + timedelta(days=days)


def _activate_step_sla(step, started_at=None, reset=True):
    """Start the SLA clock when a pending step becomes the active step."""
    if not step or getattr(step, "status", None) != "PENDING":
        return None
    days = _effective_sla_days(None, getattr(step, "sla_days", None))
    step.sla_days = days
    if reset or not getattr(step, "due_at", None):
        step.due_at = _step_due_at(None, days, started_at=started_at)
    return step.due_at


# =========================
# Approver resolution
# =========================
from sqlalchemy import or_


def _norm_role(value: str | None) -> str:
    s = (value or '').strip().lower()
    if not s:
        return ''
    s = s.replace('-', '_').replace(' ', '_')
    while '__' in s:
        s = s.replace('__', '_')
    return s.strip('_')


def _role_variants(role: str | None) -> list[str]:
    raw = (role or '').strip()
    if not raw:
        return []
    base = _norm_role(raw)
    variants = {
        raw,
        raw.lower(),
        raw.upper(),
        base,
        base.replace('_', ' '),
        base.replace('_', '-'),
        base.replace('_', ''),
    }
    if '_' in raw:
        variants.add(raw.replace('_', ' '))
        variants.add(raw.replace('_', '-'))
    if '-' in raw:
        variants.add(raw.replace('-', '_'))
        variants.add(raw.replace('-', ' '))
    if ' ' in raw:
        variants.add(raw.replace(' ', '_'))
        variants.add(raw.replace(' ', '-'))
    return [v.strip() for v in variants if v and str(v).strip()]


def _resolve_committee_users(committee_id: int | None, delivery_mode: str | None) -> list[int]:
    """Resolve committee members to concrete user ids.

    delivery_mode:
      - Committee_ALL (default)
      - Committee_CHAIR
      - Committee_SECRETARY
    """
    if not committee_id:
        return []

    mode = (delivery_mode or 'Committee_ALL').strip().upper()
    q = CommitteeAssignee.query.filter_by(committee_id=int(committee_id), is_active=True)

    if mode == 'COMMITTEE_CHAIR':
        q = q.filter(or_(CommitteeAssignee.member_role == 'CHAIR', CommitteeAssignee.member_role == 'chair'))
    elif mode == 'COMMITTEE_SECRETARY':
        q = q.filter(or_(CommitteeAssignee.member_role == 'SECRETARY', CommitteeAssignee.member_role == 'secretary'))

    members = q.all()
    user_ids: set[int] = set()

    for m in members:
        kind = (getattr(m, 'kind', '') or '').strip().upper()
        if kind == 'USER' and getattr(m, 'user_id', None):
            try:
                user_ids.add(int(m.user_id))
            except Exception:
                pass
        elif kind == 'ROLE' and getattr(m, 'role', None):
            role = (m.role or '').strip()
            vars_ = _role_variants(role)
            uq = User.query
            if vars_:
                uq = uq.filter(or_(*[User.role.ilike(v) for v in vars_]))
            else:
                uq = uq.filter(User.role.ilike(role))
            for u in uq.all():
                user_ids.add(int(u.id))

    return sorted(user_ids)


def _resolve_users_by_kind(kind: str, user_id=None, role=None, dept_id=None, dir_id=None, committee_id=None, committee_delivery_mode=None, unit_id=None, section_id=None, division_id=None, org_node_id=None):
    """Resolve user_ids for a *kind* using the SAME resolver logic.

    NOTE: For DEPARTMENT/DIRECTORATE we resolve heads (dept_head/directorate_head).
    """
    kind = (kind or '').upper().strip()

    def _resolve_org_manager_ids(unit_type: str, unit_id_val):
        try:
            uid = int(unit_id_val) if unit_id_val is not None else None
        except Exception:
            uid = None
        if not uid:
            return []

        ut = (unit_type or '').upper().strip()
        row = None
        try:
            if ut == 'ORG_NODE':
                row = OrgNodeManager.query.filter_by(node_id=uid).first()
            else:
                row = OrgUnitManager.query.filter_by(unit_type=ut, unit_id=uid).first()
        except Exception:
            row = None
        if not row:
            return []
        ids: list[int] = []
        if getattr(row, 'manager_user_id', None):
            ids.append(int(row.manager_user_id))
        if getattr(row, 'deputy_user_id', None):
            ids.append(int(row.deputy_user_id))
        return ids

    def _norm_ar_text(value: str | None) -> str:
        s = (value or '').strip().lower()
        for a, b in (
            ('أ', 'ا'), ('إ', 'ا'), ('آ', 'ا'),
            ('ى', 'ي'), ('ة', 'ه'), ('ؤ', 'و'), ('ئ', 'ي'),
        ):
            s = s.replace(a, b)
        return ''.join(ch for ch in s if not ch.isspace())

    def _is_all_directorates_record(row) -> bool:
        name = _norm_ar_text(getattr(row, 'name_ar', None) or getattr(row, 'name_en', None))
        code = (getattr(row, 'code', None) or '').strip().lower()
        return (
            ('جميع' in name and 'ادار' in name)
            or ('كل' in name and 'ادار' in name)
            or ('all' in code and 'director' in code)
        )

    def _resolve_directorate_users(directorate_id: int) -> list[int]:
        did = int(directorate_id)

        ids = _resolve_org_manager_ids('DIRECTORATE', did)
        if ids:
            return ids

        role_vars = _role_variants('directorate_head') + _role_variants('directorate_deputy')
        role_vars = sorted({v for v in role_vars if v})

        dept_ids: list[int] = []
        try:
            dept_ids = [
                int(dept_id_row) for (dept_id_row,) in (
                    db.session.query(Department.id)
                    .filter(Department.directorate_id == did)
                    .all()
                )
                if dept_id_row
            ]
        except Exception:
            dept_ids = []

        scope = [User.directorate_id == did]
        if dept_ids:
            scope.append(User.department_id.in_(dept_ids))

        q = User.query.filter(or_(*scope))
        if role_vars:
            q = q.filter(or_(*[User.role.ilike(v) for v in role_vars]))
        else:
            q = q.filter(User.role.ilike('directorate_head'))

        return sorted({int(u.id) for u in q.all() if u and getattr(u, 'id', None)})

    def _resolve_all_directorates_users(aggregate_id: int) -> list[int]:
        out: list[int] = []
        try:
            dirs = (
                Directorate.query
                .filter(Directorate.is_active.is_(True))
                .filter(Directorate.id != int(aggregate_id))
                .order_by(Directorate.id.asc())
                .all()
            )
        except Exception:
            dirs = []

        for d in dirs:
            if _is_all_directorates_record(d):
                continue
            out.extend(_resolve_directorate_users(int(d.id)))
        return sorted({int(uid) for uid in out if uid})


    if kind == 'USER' and user_id:
        return [int(user_id)]

    if kind == 'ROLE' and role:
        vars_ = _role_variants(role)
        q = User.query
        if vars_:
            q = q.filter(or_(*[User.role.ilike(v) for v in vars_]))
        else:
            q = q.filter(User.role.ilike((role or '').strip()))
        users = q.all()
        return [int(u.id) for u in users]

    if kind == 'DIRECTORATE' and dir_id:
        did = int(dir_id)
        try:
            directorate = Directorate.query.get(did)
        except Exception:
            directorate = None
        if directorate and _is_all_directorates_record(directorate):
            return _resolve_all_directorates_users(did)
        return _resolve_directorate_users(did)

    if kind == 'UNIT' and unit_id:
        return _resolve_org_manager_ids('UNIT', unit_id)

    if kind == 'SECTION' and section_id:
        return _resolve_org_manager_ids('SECTION', section_id)

    if kind == 'DIVISION' and division_id:
        return _resolve_org_manager_ids('DIVISION', division_id)
    if kind == 'ORG_NODE' and org_node_id:
        return _resolve_org_manager_ids('ORG_NODE', org_node_id)


    if kind == 'DEPARTMENT' and dept_id:
        # Prefer OrgUnitManager if configured
        ids = _resolve_org_manager_ids('DEPARTMENT', dept_id)
        if ids:
            return ids

        # Approved organization charts use OrgNodeManager, while older saved
        # workflow templates still point at Department ids.  Resolve the
        # legacy department to its dynamic/canonical node before falling back
        # to the old role-based lookup.  This also honors approved aliases
        # such as "دائرة التربية" -> "دائرة التربية والتعليم العالي".
        try:
            department_id = int(dept_id)
            node = OrgNode.query.filter_by(
                legacy_type='DEPARTMENT',
                legacy_id=department_id,
                is_active=True,
            ).first()
            if node is not None:
                ids = _resolve_org_manager_ids('ORG_NODE', node.id)
                if ids:
                    return ids

            # A legacy sync node may exist without a configured manager while
            # the approved canonical node holds the real manager/deputy.
            department = db.session.get(Department, department_id)
            if department is not None:
                from utils.approved_org_structure import find_approved_org_node_by_name
                approved_node = find_approved_org_node_by_name(
                    getattr(department, 'name_ar', None),
                    'DEPARTMENT',
                )
                if approved_node is not None:
                    ids = _resolve_org_manager_ids('ORG_NODE', approved_node.id)
                    if ids:
                        return ids
        except Exception:
            pass

        # Fallback: role-based (legacy)
        users = User.query.filter(User.department_id == int(dept_id), User.role.ilike('dept_head')).all()
        return [int(u.id) for u in users]

    if kind == 'COMMITTEE' and committee_id:
        return _resolve_committee_users(int(committee_id), committee_delivery_mode)

    return []


def _resolve_approver_users(step: WorkflowInstanceStep):
    """Returns list of user_ids to notify using the current resolver."""
    return _resolve_users_by_kind(
        getattr(step, 'approver_kind', None),
        user_id=getattr(step, 'approver_user_id', None),
        role=getattr(step, 'approver_role', None),
        dept_id=getattr(step, 'approver_department_id', None),
        dir_id=getattr(step, 'approver_directorate_id', None),
        unit_id=getattr(step, 'approver_unit_id', None),
        section_id=getattr(step, 'approver_section_id', None),
        division_id=getattr(step, 'approver_division_id', None),
        org_node_id=getattr(step, 'approver_org_node_id', None),
        committee_id=getattr(step, 'approver_committee_id', None),
        committee_delivery_mode=getattr(step, 'committee_delivery_mode', None),
    )


def resolve_step_approver_user_ids(step: WorkflowInstanceStep) -> list[int]:
    """Public, normalized view of the users currently responsible for a step."""
    return sorted({int(user_id) for user_id in _resolve_approver_users(step) if user_id})


def _step_routing_node_id(step: WorkflowInstanceStep | None) -> int | None:
    """Resolve the frozen hierarchy node represented by a runtime step."""
    if not step:
        return None

    direct_node_id = getattr(step, "approver_org_node_id", None)
    if direct_node_id:
        return int(direct_node_id)

    routing_label = (getattr(step, "routing_node_label", None) or "").strip()
    approver_user_id = getattr(step, "approver_user_id", None)
    if not routing_label or not approver_user_id:
        return None

    assignments = (
        OrgNodeManager.query
        .filter(
            (OrgNodeManager.manager_user_id == int(approver_user_id))
            | (OrgNodeManager.deputy_user_id == int(approver_user_id))
        )
        .order_by(OrgNodeManager.node_id.asc())
        .all()
    )
    if not assignments:
        return None

    try:
        from workflow.dynamic_paths import node_path_label

        exact = next(
            (
                assignment for assignment in assignments
                if assignment.node and node_path_label(assignment.node) == routing_label
            ),
            None,
        )
        if exact:
            return int(exact.node_id)
    except Exception:
        pass

    if len(assignments) == 1:
        return int(assignments[0].node_id)
    return None


def _is_strict_ancestor_node(ancestor_node_id: int, descendant_node_id: int) -> bool:
    """Return whether *ancestor* is above *descendant* in the saved hierarchy."""
    ancestor_node_id = int(ancestor_node_id)
    current_id = int(descendant_node_id)
    seen: set[int] = set()
    while current_id and current_id not in seen:
        seen.add(current_id)
        node = db.session.get(OrgNode, current_id)
        if not node or not node.parent_id:
            return False
        current_id = int(node.parent_id)
        if current_id == ancestor_node_id:
            return True
    return False


def _hierarchy_bypass_steps(
    inst: WorkflowInstance,
    target_step: WorkflowInstanceStep,
) -> list[WorkflowInstanceStep]:
    """Return lower pending steps that a future higher step may bypass."""
    current_order = int(getattr(inst, "current_step_order", 0) or 0)
    target_order = int(getattr(target_step, "step_order", 0) or 0)
    if current_order <= 0 or target_order <= current_order:
        return []
    if getattr(inst, "template_id", None) is not None:
        return []
    if (getattr(target_step, "mode", "") or "").strip().upper() != "SEQUENTIAL":
        return []
    if (getattr(target_step, "status", "") or "").strip().upper() != "PENDING":
        return []
    if (getattr(target_step, "routing_reason", "") or "").strip() == DYNAMIC_RETURN_REASON:
        return []

    route_steps = (
        WorkflowInstanceStep.query
        .filter(
            WorkflowInstanceStep.instance_id == int(inst.id),
            WorkflowInstanceStep.step_order >= current_order,
            WorkflowInstanceStep.step_order <= target_order,
        )
        .order_by(WorkflowInstanceStep.step_order.asc())
        .all()
    )
    if not route_steps or int(route_steps[0].step_order) != current_order:
        return []
    if int(route_steps[-1].id) != int(target_step.id):
        return []

    current_node_id = _step_routing_node_id(route_steps[0])
    target_node_id = _step_routing_node_id(target_step)
    if not current_node_id or not target_node_id:
        return []
    if not _is_strict_ancestor_node(target_node_id, current_node_id):
        return []

    previous_node_id = current_node_id
    bypassed: list[WorkflowInstanceStep] = []
    for index, route_step in enumerate(route_steps):
        status = (getattr(route_step, "status", "") or "").strip().upper()
        if status == "SKIPPED":
            continue
        if status != "PENDING":
            return []
        if (getattr(route_step, "mode", "") or "").strip().upper() != "SEQUENTIAL":
            return []
        if (getattr(route_step, "routing_reason", "") or "").strip() == DYNAMIC_RETURN_REASON:
            return []

        node_id = _step_routing_node_id(route_step)
        if not node_id:
            return []
        if index and node_id != previous_node_id:
            if not _is_strict_ancestor_node(node_id, previous_node_id):
                return []
        previous_node_id = node_id
        if int(route_step.id) != int(target_step.id):
            bypassed.append(route_step)

    return bypassed


def resolve_hierarchy_bypass_step(
    inst: WorkflowInstance,
    actor_user_ids,
) -> WorkflowInstanceStep | None:
    """Find the nearest future higher step an actor may execute immediately."""
    if not inst or getattr(inst, "is_completed", False):
        return None
    actor_ids = {int(user_id) for user_id in (actor_user_ids or []) if user_id}
    if not actor_ids:
        return None

    candidates = (
        WorkflowInstanceStep.query
        .filter(
            WorkflowInstanceStep.instance_id == int(inst.id),
            WorkflowInstanceStep.step_order > int(inst.current_step_order or 0),
            WorkflowInstanceStep.status == "PENDING",
        )
        .order_by(WorkflowInstanceStep.step_order.asc())
        .all()
    )
    for candidate in candidates:
        if not actor_ids.intersection(resolve_step_approver_user_ids(candidate)):
            continue
        if _hierarchy_bypass_steps(inst, candidate):
            return candidate
    return None


def _resolve_parallel_extra_assignees(template_id: int | None, step_order: int) -> list[int]:
    """Extra assignees linked to a PARALLEL_SYNC step number (template step)."""
    try:
        if not template_id:
            return []
        ts = (
            WorkflowTemplateStep.query
            .filter_by(template_id=int(template_id), step_order=int(step_order))
            .first()
        )
        if not ts:
            return []
        rows = (
            WorkflowTemplateParallelAssignee.query
            .filter(
                or_(
                    WorkflowTemplateParallelAssignee.template_step_id == ts.id,
                    (
                        (WorkflowTemplateParallelAssignee.template_id == int(template_id))
                        & (WorkflowTemplateParallelAssignee.step_order == int(step_order))
                    ),
                )
            )
            .all()
        )
        out: list[int] = []
        for r in rows:
            out.extend(_resolve_users_by_kind(
                getattr(r, 'approver_kind', None),
                user_id=getattr(r, 'approver_user_id', None),
                role=getattr(r, 'approver_role', None),
                dept_id=getattr(r, 'approver_department_id', None),
                dir_id=getattr(r, 'approver_directorate_id', None),
                unit_id=getattr(r, 'approver_unit_id', None),
                section_id=getattr(r, 'approver_section_id', None),
                division_id=getattr(r, 'approver_division_id', None),
                org_node_id=getattr(r, 'approver_org_node_id', None),
                committee_id=getattr(r, 'approver_committee_id', None),
                committee_delivery_mode=getattr(r, 'committee_delivery_mode', None),
            ))
        return sorted({int(x) for x in out if x})
    except Exception:
        return []


def resolve_template_participant_user_ids(template: WorkflowTemplate) -> set[int]:
    """Resolve every user currently targeted by a workflow template."""
    participant_ids: set[int] = set()
    if not template:
        return participant_ids

    for step in list(getattr(template, "steps", None) or []):
        participant_ids.update(_resolve_users_by_kind(
            getattr(step, "approver_kind", None),
            user_id=getattr(step, "approver_user_id", None),
            role=getattr(step, "approver_role", None),
            dept_id=getattr(step, "approver_department_id", None),
            dir_id=getattr(step, "approver_directorate_id", None),
            unit_id=getattr(step, "approver_unit_id", None),
            section_id=getattr(step, "approver_section_id", None),
            division_id=getattr(step, "approver_division_id", None),
            org_node_id=getattr(step, "approver_org_node_id", None),
            committee_id=getattr(step, "approver_committee_id", None),
            committee_delivery_mode=getattr(step, "committee_delivery_mode", None),
        ))
        participant_ids.update(_resolve_parallel_extra_assignees(
            getattr(template, "id", None),
            getattr(step, "step_order", 0),
        ))

    return {int(user_id) for user_id in participant_ids if user_id}


def resolve_template_parallel_candidate_user_ids(
    template: WorkflowTemplate,
    step_order: int,
) -> list[int]:
    """Resolve the eligible candidate pool for one parallel template step."""
    if not template:
        return []
    step = next(
        (
            row for row in (getattr(template, "steps", None) or [])
            if int(getattr(row, "step_order", 0) or 0) == int(step_order)
        ),
        None,
    )
    if not step or (getattr(step, "mode", "") or "").strip().upper() != "PARALLEL_SYNC":
        return []

    candidate_ids = set(_resolve_users_by_kind(
        getattr(step, "approver_kind", None),
        user_id=getattr(step, "approver_user_id", None),
        role=getattr(step, "approver_role", None),
        dept_id=getattr(step, "approver_department_id", None),
        dir_id=getattr(step, "approver_directorate_id", None),
        unit_id=getattr(step, "approver_unit_id", None),
        section_id=getattr(step, "approver_section_id", None),
        division_id=getattr(step, "approver_division_id", None),
        org_node_id=getattr(step, "approver_org_node_id", None),
        committee_id=getattr(step, "approver_committee_id", None),
        committee_delivery_mode=getattr(step, "committee_delivery_mode", None),
    ))
    candidate_ids.update(_resolve_parallel_extra_assignees(
        getattr(template, "id", None),
        int(step_order),
    ))
    return sorted(int(user_id) for user_id in candidate_ids if user_id)


def _notify_users(
    user_ids,
    message,
    ntype="WORKFLOW",
    role=None,
    actor_id=None,
    track_for_actor=False,
    req: WorkflowRequest | None = None,
    task_assignment: bool = False,
    step_order: int | None = None,
    instance_id: int | None = None,
):
    """
    Your Notification model has: message, type, role, is_read, created_at
    (no title/url) => keep it compatible.
    """
    if not user_ids:
        return

    now = datetime.utcnow()
    event_key = uuid.uuid4().hex
    unique_ids = set(int(uid) for uid in user_ids if uid)
    link_url = notification_target_path("WorkflowRequest", req.id) if req is not None else None
    if req is not None:
        unique_ids = filter_confidential_workflow_user_ids(req, unique_ids)

    # Recipient notifications
    for uid in unique_ids:
        db.session.add(
            Notification(
                user_id=int(uid),
                type=ntype,
                role=role,
                message=message,
                is_read=False,
                created_at=now,
                actor_id=actor_id,
                event_key=event_key,
                is_mirror=False,
                source="workflow",
                link_url=link_url,
                email_delivery_mode="TASK_ASSIGNMENT" if task_assignment else "GENERAL",
            )
        )

    if task_assignment and req is not None and step_order:
        from services.workflow_task_email import enqueue_task_assignment_emails

        enqueue_task_assignment_emails(
            req,
            unique_ids,
            step_order=int(step_order),
            instance_id=instance_id,
            link_url=link_url,
        )

    # Sender mirror notification (tracks recipients' read)
    if track_for_actor and actor_id and int(actor_id) not in unique_ids:
        db.session.add(
            Notification(
                user_id=int(actor_id),
                type=ntype,
                role=role,
                message=f"متابعة: {message}",
                is_read=False,
                created_at=now,
                actor_id=int(actor_id),
                event_key=event_key,
                is_mirror=True,
                source="workflow",
                link_url=link_url,
            )
        )


# =========================
# Parallel Sync helpers
# =========================

def _is_parallel_sync(step: WorkflowInstanceStep) -> bool:
    return (getattr(step, "mode", "SEQUENTIAL") or "SEQUENTIAL") == "PARALLEL_SYNC"


def can_committee_chair_bypass_parallel_step(
    user_id: int | None,
    step: WorkflowInstanceStep | None,
) -> bool:
    """Return whether a committee chair may bypass other committee members.

    The authority is limited to active PARALLEL_SYNC steps delivered to all
    members of the committee.  It does not apply to chair-only, secretary-only,
    or non-committee steps.
    """
    if not user_id or not step or not _is_parallel_sync(step):
        return False
    if (getattr(step, "approver_kind", "") or "").strip().upper() != "COMMITTEE":
        return False

    delivery_mode = (
        getattr(step, "committee_delivery_mode", None) or "Committee_ALL"
    ).strip().upper()
    if delivery_mode != "COMMITTEE_ALL":
        return False

    try:
        chair_ids = _resolve_committee_users(
            getattr(step, "approver_committee_id", None),
            "Committee_CHAIR",
        )
        return int(user_id) in set(chair_ids)
    except (TypeError, ValueError):
        return False


def resolve_parallel_candidate_user_ids(
    req: WorkflowRequest,
    inst: WorkflowInstance,
    step: WorkflowInstanceStep,
) -> list[int]:
    """Resolve the users eligible for an instance's PARALLEL_SYNC step.

    Eligibility comes from the template/runtime step and the correspondence
    confidentiality gate.  Eligibility alone does not grant workflow access;
    the previous-step actor must explicitly authorize a subset first.
    """
    if not _is_parallel_sync(step):
        return []

    assignees = _resolve_approver_users(step)
    assignees += _resolve_parallel_extra_assignees(
        getattr(inst, "template_id", None),
        step.step_order,
    )
    return sorted(filter_confidential_workflow_user_ids(
        req,
        {int(uid) for uid in assignees if uid},
    ))


def resolve_dynamic_branch_steps(
    inst: WorkflowInstance,
    current_step: WorkflowInstanceStep,
) -> list[WorkflowInstanceStep]:
    """Return sibling dynamic targets that the shared parent manager must route to.

    Dynamic workflows are stored as a flat sequence.  When two or more
    consecutive ORG_NODE targets share the same parent, the manager of that
    parent is the routing decision point.  The manager selects one or more
    targets; the remaining sibling targets are skipped for this request.
    """
    if not inst or not current_step or getattr(inst, "template_id", None) is not None:
        return []
    if (getattr(current_step, "status", "") or "").strip().upper() != "PENDING":
        return []
    if _is_parallel_sync(current_step):
        return []

    following_steps = (
        WorkflowInstanceStep.query
        .filter(
            WorkflowInstanceStep.instance_id == int(inst.id),
            WorkflowInstanceStep.step_order > int(current_step.step_order),
            WorkflowInstanceStep.status == "PENDING",
        )
        .order_by(WorkflowInstanceStep.step_order.asc())
        .all()
    )
    if not following_steps:
        return []

    expected_order = int(current_step.step_order) + 1
    first_step = following_steps[0]
    if int(first_step.step_order) != expected_order:
        return []
    if (getattr(first_step, "routing_reason", "") or "").strip() == DYNAMIC_RETURN_REASON:
        return []
    if (getattr(first_step, "approver_kind", "") or "").strip().upper() != "ORG_NODE":
        return []

    first_node = db.session.get(OrgNode, int(first_step.approver_org_node_id or 0))
    parent_id = int(getattr(first_node, "parent_id", 0) or 0)
    if not parent_id:
        return []

    candidates: list[WorkflowInstanceStep] = []
    for candidate in following_steps:
        if int(candidate.step_order) != expected_order:
            break
        if (getattr(candidate, "routing_reason", "") or "").strip() == DYNAMIC_RETURN_REASON:
            break
        if (getattr(candidate, "approver_kind", "") or "").strip().upper() != "ORG_NODE":
            break
        node = db.session.get(OrgNode, int(candidate.approver_org_node_id or 0))
        if not node or int(getattr(node, "parent_id", 0) or 0) != parent_id:
            break
        candidates.append(candidate)
        expected_order += 1

    if len(candidates) < 2:
        return []

    parent_manager_ids = set(_resolve_users_by_kind("ORG_NODE", org_node_id=parent_id))
    current_approver_ids = set(_resolve_approver_users(current_step))
    if not parent_manager_ids.intersection(current_approver_ids):
        return []
    return candidates


def _ensure_parallel_tasks(
    req: WorkflowRequest,
    inst: WorkflowInstance,
    step: WorkflowInstanceStep,
    authorized_user_ids=None,
):
    """Create authorized per-assignee tasks for a PARALLEL_SYNC step.

    A task is the runtime access grant for a parallel participant.  When no
    explicit authorization is supplied, this function only preserves existing
    tasks; it never expands the template's full candidate list automatically.
    """
    if not _is_parallel_sync(step):
        return

    now = datetime.utcnow()
    candidate_ids = set(resolve_parallel_candidate_user_ids(req, inst, step))

    existing_tasks = (
        WorkflowStepTask.query
        .filter_by(instance_id=inst.id, step_order=step.step_order)
        .all()
    )
    existing_ids = {
        int(task.assignee_user_id)
        for task in existing_tasks
        if getattr(task, "assignee_user_id", None)
    }
    mention_task_ids = {
        int(task.assignee_user_id)
        for task in existing_tasks
        if getattr(task, "assignee_user_id", None) and is_mention_task(task)
    }

    if authorized_user_ids is None:
        # Existing workflows/tasks remain valid, but merely appearing in the
        # template must not grant a new participant access.
        desired_ids = existing_ids.intersection(candidate_ids)
    else:
        try:
            desired_ids = {int(uid) for uid in authorized_user_ids if int(uid) > 0}
        except (TypeError, ValueError):
            raise ValueError("قائمة المخولين في الخطوة المتزامنة غير صالحة")

        invalid_ids = desired_ids.difference(candidate_ids)
        if invalid_ids:
            raise ValueError("تتضمن قائمة التوجيه مستخدمين غير مرشحين للخطوة المتزامنة")
        if not desired_ids:
            raise ValueError("يجب اختيار شخص واحد على الأقل للخطوة المتزامنة")

    # A comment mention is an explicit, audit-backed addition to the active
    # workflow. It is not constrained to the template candidate list, so it
    # must never be interpreted as an unauthorized task and marked BYPASSED
    # when this page is rendered or the workflow is reloaded.
    desired_ids.update(mention_task_ids)

    # Legacy secret workflows may already contain pending tasks for users who
    # are outside the ACL.  Hide-and-leave would deadlock a parallel step, so
    # preserve the row for audit while removing it from the pending quorum.
    unauthorized_existing = existing_ids.difference(desired_ids)
    if unauthorized_existing:
        (
            WorkflowStepTask.query
            .filter(
                WorkflowStepTask.instance_id == inst.id,
                WorkflowStepTask.step_order == step.step_order,
                WorkflowStepTask.assignee_user_id.in_(unauthorized_existing),
                WorkflowStepTask.status == "PENDING",
            )
            .update(
                {
                    "status": "BYPASSED",
                    "bypass_reason": "Removed from confidential workflow by source ACL",
                    "bypassed_at": now,
                },
                synchronize_session=False,
            )
        )
        existing_ids.intersection_update(desired_ids)

    missing_ids = sorted(desired_ids - existing_ids)
    created_ids: list[int] = []

    # Use a SAVEPOINT so a unique-constraint race won't rollback the whole outer transaction.
    if missing_ids:
        try:
            with db.session.begin_nested():
                for uid in missing_ids:
                    db.session.add(
                        WorkflowStepTask(
                            instance_id=inst.id,
                            request_id=req.id,
                            step_order=step.step_order,
                            assignee_user_id=uid,
                            status="PENDING",
                            response="NONE",
                            created_at=now,
                        )
                    )
                db.session.flush()
            created_ids = missing_ids
            existing_ids.update(created_ids)
        except IntegrityError:
            db.session.expire_all()
            existing_ids = {
                int(uid) for (uid,) in (
                    db.session.query(WorkflowStepTask.assignee_user_id)
                    .filter_by(instance_id=inst.id, step_order=step.step_order)
                    .all()
                )
                if uid
            }

    notify_ids: list[int] = []
    regular_existing_ids = existing_ids.difference(mention_task_ids)
    if regular_existing_ids and not getattr(step, "parallel_notified_at", None):
        notify_ids = sorted(regular_existing_ids)
    elif created_ids:
        notify_ids = created_ids

    # Notify all initial assignees once, and notify any assignees added after activation.
    if notify_ids:
        _notify_users(
            notify_ids,
            f"مهمة متزامنة للطلب #{req.id}: يرجى الرد (للتوثيق فقط).",
            ntype="WORKFLOW",
            req=req,
            task_assignment=True,
            step_order=step.step_order,
            instance_id=inst.id,
        )
        if not getattr(step, "parallel_notified_at", None):
            step.parallel_notified_at = now
            db.session.add(step)


def ensure_parallel_tasks(req: WorkflowRequest, inst: WorkflowInstance, step: WorkflowInstanceStep):
    """Preserve/render already-authorized PARALLEL_SYNC task status."""
    return _ensure_parallel_tasks(req, inst, step)


def authorize_parallel_step(
    req: WorkflowRequest,
    inst: WorkflowInstance,
    step: WorkflowInstanceStep,
    authorized_user_ids,
    actor_user_id: int,
    effective_user_id: int | None = None,
    on_behalf_of_id: int | None = None,
    delegation_id: int | None = None,
    auto_commit: bool = False,
) -> list[int]:
    """Authorize and activate a PARALLEL_SYNC step exactly once.

    Authorization belongs to the actor who completed the previous step (or an
    administrator).  Only selected candidates receive tasks and notifications.
    """
    if not _is_parallel_sync(step):
        raise ValueError("هذه ليست خطوة متزامنة")
    if int(getattr(inst, "current_step_order", 0) or 0) != int(step.step_order):
        raise ValueError("الخطوة المتزامنة ليست نشطة حاليًا")
    if (getattr(step, "status", "") or "").upper() != "PENDING":
        raise ValueError("الخطوة المتزامنة ليست قيد الانتظار")

    effective_user_id = int(effective_user_id or actor_user_id)
    actor_user = db.session.get(User, int(actor_user_id))
    effective_user = db.session.get(User, effective_user_id)
    is_admin = bool(
        (actor_user and (actor_user.has_role("ADMIN") or actor_user.has_role("SUPER_ADMIN")))
        or (effective_user and (effective_user.has_role("ADMIN") or effective_user.has_role("SUPER_ADMIN")))
    )
    if not is_admin and int(getattr(inst, "last_step_actor_id", 0) or 0) != effective_user_id:
        raise PermissionError("غير مخوّل بتوجيه هذه الخطوة المتزامنة")

    existing_count = WorkflowStepTask.query.filter_by(
        instance_id=inst.id,
        step_order=step.step_order,
    ).count()
    if existing_count or getattr(step, "parallel_notified_at", None):
        raise ValueError("تم توجيه الخطوة المتزامنة مسبقًا")

    candidate_ids = set(resolve_parallel_candidate_user_ids(req, inst, step))
    try:
        selected_ids = {int(uid) for uid in authorized_user_ids if int(uid) > 0}
    except (TypeError, ValueError):
        raise ValueError("قائمة المخولين في الخطوة المتزامنة غير صالحة")
    if not selected_ids:
        raise ValueError("يجب اختيار شخص واحد على الأقل للخطوة المتزامنة")
    if not selected_ids.issubset(candidate_ids):
        raise ValueError("يمكن التوجيه فقط إلى المرشحين المحددين في قالب المسار")

    _ensure_parallel_tasks(
        req,
        inst,
        step,
        authorized_user_ids=selected_ids,
    )
    selected = sorted(selected_ids)
    selected_users = User.query.filter(User.id.in_(selected)).all()
    selected_labels = sorted(
        (user.full_name or user.email or f"مستخدم #{user.id}")
        for user in selected_users
    )
    db.session.add(AuditLog(
        request_id=req.id,
        user_id=int(actor_user_id),
        on_behalf_of_id=on_behalf_of_id,
        delegation_id=delegation_id,
        action="PARALLEL_SYNC_AUTHORIZED",
        old_status=None,
        new_status=None,
        note=(
            f"الخطوة المتزامنة {step.step_order}: تم توجيهها إلى: "
            + "، ".join(selected_labels)
        ),
        target_type="WORKFLOW_INSTANCE_STEP",
        target_id=step.id,
    ))

    if auto_commit:
        db.session.commit()
    return selected


def _parallel_is_complete(inst_id: int, step_order: int) -> bool:
    pending = (
        WorkflowStepTask.query
        .filter_by(instance_id=inst_id, step_order=step_order, status="PENDING")
        .count()
    )
    return pending == 0


def _parallel_total(inst_id: int, step_order: int) -> int:
    return (
        WorkflowStepTask.query
        .filter_by(instance_id=inst_id, step_order=step_order)
        .count()
    )


def _resolve_followers_user_ids(inst_id: int) -> list[int]:
    """Users who decided or were retained as followers after a hierarchy bypass."""
    rows = (
        db.session.query(WorkflowInstanceStep.decided_by_id)
        .filter(WorkflowInstanceStep.instance_id == inst_id)
        .filter(WorkflowInstanceStep.decided_by_id.isnot(None))
        .all()
    )
    ids: set[int] = set()
    for (uid,) in rows:
        try:
            if uid:
                ids.add(int(uid))
        except Exception:
            pass

    inst = db.session.get(WorkflowInstance, int(inst_id))
    if inst:
        watcher_rows = (
            db.session.query(AuditLog.target_id)
            .filter(
                AuditLog.request_id == int(inst.request_id),
                AuditLog.action == HIERARCHY_BYPASS_FOLLOWER_ACTION,
                AuditLog.target_type == "USER",
                AuditLog.target_id.isnot(None),
            )
            .all()
        )
        for (uid,) in watcher_rows:
            try:
                if uid:
                    ids.add(int(uid))
            except Exception:
                pass
    return sorted(ids)


# =========================
# Engine API
# =========================
def start_workflow_for_request(
    req: WorkflowRequest,
    template: WorkflowTemplate | None,
    created_by_user_id: int,
    auto_commit: bool = False,
    initial_parallel_user_ids=None,
    runtime_steps: list[dict] | None = None,
    workflow_label: str | None = None,
):
    """
    Creates workflow instance + instance steps from template.
    Sets first step due_at, and notifies approvers.
    """
    inst = WorkflowInstance(
        request_id=req.id,
        template_id=template.id if template else None,
        current_step_order=1
    )
    # For PARALLEL_SYNC: the request creator is considered the "previous step actor"
    # for the first step (thus can bypass if step 1 is parallel).
    inst.last_step_actor_id = int(created_by_user_id) if created_by_user_id else None
    db.session.add(inst)
    db.session.flush()  # get inst.id

    template_sla = template.sla_days_default if template else None

    # IMPORTANT: in your models, template.steps is a LIST (not dynamic query)
    # and already ordered by step_order (relationship order_by).
    tsteps = list(runtime_steps if runtime_steps is not None else (template.steps or [] if template else []))
    if runtime_steps is not None and not tsteps:
        raise ValueError("المسار الديناميكي لا يحتوي على خطوات.")

    def step_value(step_source, field_name, default=None):
        if isinstance(step_source, dict):
            return step_source.get(field_name, default)
        return getattr(step_source, field_name, default)

    for runtime_order, ts in enumerate(tsteps, start=1):
        # Defensive normalization (some seeded/legacy data may store lowercase/extra spaces)
        _kind = ((step_value(ts, 'approver_kind') or '').strip().upper())
        if _kind not in ("USER", "ROLE", "DEPARTMENT", "DIRECTORATE", "UNIT", "SECTION", "DIVISION", "ORG_NODE", "COMMITTEE"):
            _kind = ""

        _cmode = step_value(ts, 'committee_delivery_mode')
        if _cmode:
            _cm = str(_cmode).strip()
            # accept both canonical and uppercase aliases
            up = _cm.upper()
            if up == 'COMMITTEE_ALL':
                _cmode = 'Committee_ALL'
            elif up == 'COMMITTEE_CHAIR':
                _cmode = 'Committee_CHAIR'
            elif up == 'COMMITTEE_SECRETARY':
                _cmode = 'Committee_SECRETARY'

        effective_sla_days = _effective_sla_days(
            template_sla,
            step_value(ts, 'sla_days'),
        )
        step = WorkflowInstanceStep(
            instance_id=inst.id,
            step_order=int(step_value(ts, "step_order", runtime_order) or runtime_order),
            mode=step_value(ts, "mode", "SEQUENTIAL") or "SEQUENTIAL",
            approver_kind=_kind,
            approver_user_id=step_value(ts, 'approver_user_id'),
            approver_department_id=step_value(ts, 'approver_department_id'),
            approver_directorate_id=step_value(ts, 'approver_directorate_id'),
            approver_unit_id=step_value(ts, 'approver_unit_id'),
            approver_section_id=step_value(ts, 'approver_section_id'),
            approver_division_id=step_value(ts, 'approver_division_id'),
            approver_org_node_id=step_value(ts, 'approver_org_node_id'),
            routing_label=step_value(ts, 'label'),
            routing_job_title=step_value(ts, 'job_title'),
            routing_node_label=step_value(ts, 'node_label'),
            routing_reason=step_value(ts, 'reason'),
            approver_role=step_value(ts, 'approver_role'),
            approver_committee_id=step_value(ts, 'approver_committee_id'),
            committee_delivery_mode=_cmode,
            status="PENDING",
            sla_days=effective_sla_days,
            # A waiting step must not consume SLA time before it is active.
            due_at=(
                _step_due_at(None, effective_sla_days)
                if runtime_order == 1 else None
            ),
        )
        db.session.add(step)

    req.status = "IN_PROGRESS"
    db.session.add(req)
    db.session.flush()

    resolved_workflow_label = workflow_label or (template.name if template else "مسار ديناميكي حسب الهيكل الإداري")
    audit_note = (
        f"Template: {resolved_workflow_label} (#{template.id})"
        if template
        else f"Dynamic workflow: {resolved_workflow_label}"
    )
    db.session.add(AuditLog(
        request_id=req.id,
        user_id=created_by_user_id,
        action="WORKFLOW_STARTED",
        old_status=None,
        new_status=req.status,
        note=audit_note,
        target_type="WORKFLOW",
        target_id=inst.id
    ))

    # notify first step approvers
    first = WorkflowInstanceStep.query.filter_by(
        instance_id=inst.id,
        step_order=1
    ).first()

    if first:
        if _is_parallel_sync(first):
            if initial_parallel_user_ids is not None:
                authorize_parallel_step(
                    req,
                    inst,
                    first,
                    authorized_user_ids=initial_parallel_user_ids,
                    actor_user_id=created_by_user_id,
                    effective_user_id=created_by_user_id,
                    auto_commit=False,
                )
            else:
                _ensure_parallel_tasks(req, inst, first)
        else:
            approvers = _resolve_approver_users(first)
            _notify_users(
                approvers,
                message=f"طلب جديد يحتاج إجراء: #{req.id} (الخطوة 1)",
                ntype="WORKFLOW",
                role=first.approver_role,
                actor_id=req.requester_id,
                track_for_actor=True,
                req=req,
                task_assignment=True,
                step_order=first.step_order,
                instance_id=inst.id,
            )

    if auto_commit:
        db.session.commit()


def _bypass_parallel_task_legacy(
    request_id: int,
    step_order: int,
    actor_user_id: int,
    effective_user_id: int,
    assignee_user_id: int,
    reason: str,
    on_behalf_of_id=None,
    auto_commit: bool = True,
):
    """Bypass a pending assignee in a PARALLEL_SYNC step.

    Authorized:
    - SUPER_ADMIN / ADMIN
    - previous-step actor (WorkflowInstance.last_step_actor_id)
      using *effective_user_id* (delegation-aware).
    """

    reason = (reason or "").strip()
    if not reason:
        raise ValueError("bypass reason is required")

    req = WorkflowRequest.query.get_or_404(request_id)
    inst = WorkflowInstance.query.filter_by(request_id=request_id).first()
    if not inst:
        raise ValueError("workflow instance not found")

    if int(inst.current_step_order or 0) != int(step_order):
        raise ValueError("cannot bypass: not on this step")

    step = WorkflowInstanceStep.query.filter_by(instance_id=inst.id, step_order=step_order).first()
    if not step:
        raise ValueError("step not found")
    if step.status != "PENDING" or not _is_parallel_sync(step):
        raise ValueError("bypass is only allowed for active PARALLEL_SYNC steps")

    actor = User.query.get(actor_user_id)
    eff = User.query.get(effective_user_id)
    actor_label = actor.full_name if actor else f"User#{actor_user_id}"
    eff_label = eff.full_name if eff else f"User#{effective_user_id}"
    actor_display = actor_label if not on_behalf_of_id else f"{actor_label} (مفوّض عن {eff_label})"

    is_admin = bool(eff and (eff.has_role("SUPER_ADMIN") or eff.has_role("ADMIN")))
    if not is_admin and int(inst.last_step_actor_id or 0) != int(effective_user_id):
        raise PermissionError("not allowed to bypass this step")

    _ensure_parallel_tasks(req, inst, step)

    task = WorkflowStepTask.query.filter_by(
        instance_id=inst.id,
        step_order=step_order,
        assignee_user_id=assignee_user_id,
    ).first()
    if not task:
        raise ValueError("assignee task not found")
    if task.status != "PENDING":
        raise ValueError("cannot bypass: task is not pending")

    now = datetime.utcnow()
    task.status = "BYPASSED"
    task.bypassed_by = effective_user_id
    task.bypass_reason = reason
    task.bypassed_at = now

    db.session.add(
        AuditLog(
            action="PARALLEL_SYNC_BYPASS",
            user_id=actor_user_id,
            on_behalf_of_id=on_behalf_of_id,
            target_type="WORKFLOW_STEP_TASK",
            target_id=task.id,
            note=f"Bypass assignee {assignee_user_id} at step {step_order}. Reason: {reason}",
        )
    )

    # If step is complete, close it and advance
    if _parallel_is_complete(inst.id, step_order):
        step.status = "APPROVED"
        step.decided_by_id = effective_user_id
        step.decided_at = now
        step.note = f"Parallel sync completed via bypass by {actor_display}."
        inst.last_step_actor_id = effective_user_id

        next_order = step_order + 1
        next_step = WorkflowInstanceStep.query.filter_by(instance_id=inst.id, step_order=next_order).first()
        if not next_step:
            req.status = "APPROVED"
            inst.is_completed = True
            _notify_users(
                [req.requester_id],
                message=f"تم اعتماد الطلب #{req.id} (اكتملت جميع الخطوات)",
                ntype="WORKFLOW",
                req=req,
            )
        else:
            inst.current_step_order = next_order
            _activate_step_sla(next_step, started_at=now, reset=True)
            if _is_parallel_sync(next_step):
                _ensure_parallel_tasks(req, inst, next_step)
            else:
                approvers = _resolve_approver_users(next_step)
                _notify_users(
                    approvers,
                    message=f"طلب جديد يحتاج إجراء: #{req.id} (الخطوة {next_order})",
                    ntype="WORKFLOW",
                    role=next_step.approver_role,
                    actor_id=req.requester_id,
                    track_for_actor=True,
                    req=req,
                    task_assignment=True,
                    step_order=next_step.step_order,
                    instance_id=inst.id,
                )

            _notify_users(
                [req.requester_id],
                message=f"اكتملت الخطوة المتزامنة للطلب #{req.id} وتم تحويله للخطوة {next_order}.",
                ntype="WORKFLOW",
                req=req,
            )

    if auto_commit:
        db.session.commit()



def bypass_parallel_task(
    request_id: int,
    step_order: int,
    actor_user_id: int,
    effective_user_id: int,
    assignee_user_id: int,
    reason: str,
    on_behalf_of_id=None,
    auto_commit: bool = True,
):
    """Bypass a pending assignee in a PARALLEL_SYNC step.

    Only the previous-step actor (WorkflowInstance.last_step_actor_id) or ADMIN/SUPERADMIN
    can bypass while the parallel step is active.
    """

    req = WorkflowRequest.query.get_or_404(request_id)
    inst = WorkflowInstance.query.filter_by(request_id=request_id).first_or_404()

    step = WorkflowInstanceStep.query.filter_by(instance_id=inst.id, step_order=step_order).first_or_404()
    if not _is_parallel_sync(step):
        raise ValueError("هذه ليست خطوة متزامنة")
    if inst.current_step_order != step_order or step.status != "PENDING":
        raise ValueError("الخطوة المتزامنة ليست نشطة حاليًا")

    # Authorization: admin/superadmin OR previous-step actor (effective;
    # delegation-aware) OR the chair of a Committee_ALL step.
    actor_user = User.query.get(actor_user_id)
    eff_user = User.query.get(effective_user_id)

    is_admin = bool(actor_user and (actor_user.has_role("ADMIN") or actor_user.has_role("SUPER_ADMIN"))) or \
        bool(eff_user and (eff_user.has_role("ADMIN") or eff_user.has_role("SUPER_ADMIN")))
    is_previous_step_actor = int(inst.last_step_actor_id or 0) == int(effective_user_id)
    is_committee_chair = can_committee_chair_bypass_parallel_step(effective_user_id, step)

    if not is_admin and not is_previous_step_actor and not is_committee_chair:
        raise PermissionError("غير مخوّل بالتجاوز في هذه الخطوة")

    _ensure_parallel_tasks(req, inst, step)
    task = WorkflowStepTask.query.filter_by(
        instance_id=inst.id,
        step_order=step_order,
        assignee_user_id=int(assignee_user_id),
    ).first()
    if not task:
        raise ValueError("المستخدم غير ضمن المتزامنين")
    if task.status != "PENDING":
        raise ValueError("لا يمكن تجاوز مستخدم حالته ليست قيد الانتظار")
    if (
        is_committee_chair
        and not is_admin
        and not is_previous_step_actor
        and int(task.assignee_user_id) == int(effective_user_id)
    ):
        raise PermissionError("رئيس اللجنة لا يمكنه تجاوز مهمته الشخصية")

    now = datetime.utcnow()
    task.status = "BYPASSED"
    task.bypassed_by_id = int(effective_user_id)
    task.bypass_reason = (reason or "").strip()[:500]
    task.bypassed_at = now

    # Audit
    actor_label = (actor_user.full_name if actor_user else f"User#{actor_user_id}")
    # eff_user loaded above
    eff_label = (eff_user.full_name if eff_user else f"User#{effective_user_id}")
    actor_display = actor_label if not on_behalf_of_id else f"{actor_label} (مفوّض عن {eff_label})"
    db.session.add(
        AuditLog(
            user_id=actor_user_id,
            action="PARALLEL_SYNC_BYPASS",
            target_type="WORKFLOW_STEP_TASK",
            target_id=task.id,
            note=f"bypass step={step_order} assignee={assignee_user_id} by={actor_display} reason={task.bypass_reason}",
            on_behalf_of_id=on_behalf_of_id,
        )
    )

    # If all responded/bypassed, close step and advance
    if _parallel_is_complete(inst.id, step_order):
        step.status = "APPROVED"
        step.decided_by_id = int(effective_user_id)
        step.decided_at = now
        step.note = "تم إغلاق الخطوة المتزامنة (بسبب تجاوز/اكتمال)"

        next_order = step_order + 1
        next_step = WorkflowInstanceStep.query.filter_by(instance_id=inst.id, step_order=next_order).first()

        inst.last_step_actor_id = int(effective_user_id)

        if not next_step:
            inst.is_completed = True
            req.status = "APPROVED"
            req.completed_at = now
            db.session.add(
                AuditLog(
                    user_id=actor_user_id,
                    action="WORKFLOW_COMPLETED",
                    target_type="WORKFLOW_REQUEST",
                    target_id=req.id,
                    note=f"Workflow completed after PARALLEL_SYNC step {step_order} by {actor_display}",
                    on_behalf_of_id=on_behalf_of_id,
                )
            )
            _notify_users([req.requester_id], message=f"تم إنجاز الطلب #{req.id} ✅", ntype="WORKFLOW", actor_id=actor_user_id, track_for_actor=True, req=req)
        else:
            inst.current_step_order = next_order
            _activate_step_sla(next_step, started_at=now, reset=True)
            msg = f"اكتملت الخطوة المتزامنة للطلب #{req.id} وتم تحويله للخطوة {next_order} بواسطة {actor_display}"
            _notify_users([req.requester_id], message=msg, ntype="WORKFLOW", actor_id=actor_user_id, track_for_actor=True, req=req)

            if _is_parallel_sync(next_step):
                _ensure_parallel_tasks(req, inst, next_step)
            else:
                approvers = _resolve_approver_users(next_step)
                _notify_users(
                    approvers,
                    message=f"طلب جديد يحتاج إجراء: #{req.id} (الخطوة {next_order})",
                    ntype="WORKFLOW",
                    role=next_step.approver_role,
                    actor_id=req.requester_id,
                    track_for_actor=True,
                    req=req,
                    task_assignment=True,
                    step_order=next_step.step_order,
                    instance_id=inst.id,
                )

    if auto_commit:
        db.session.commit()

    return inst


def bypass_all_parallel_tasks(
    request_id: int,
    step_order: int,
    actor_user_id: int,
    effective_user_id: int,
    reason: str,
    on_behalf_of_id=None,
    auto_commit: bool = True,
):
    """Bypass ALL remaining PENDING assignees in an active PARALLEL_SYNC step.

    Authorized:
    - ADMIN / SUPER_ADMIN
    - Previous-step actor (WorkflowInstance.last_step_actor_id) using effective_user_id (delegation-aware)
    - Chair of a Committee_ALL step, for other members only
    """

    reason = (reason or "").strip()
    if not reason:
        raise ValueError("سبب التجاوز مطلوب")

    req = WorkflowRequest.query.get_or_404(request_id)
    inst = WorkflowInstance.query.filter_by(request_id=request_id).first_or_404()

    step = WorkflowInstanceStep.query.filter_by(instance_id=inst.id, step_order=step_order).first_or_404()
    if not _is_parallel_sync(step):
        raise ValueError("هذه ليست خطوة متزامنة")
    if inst.current_step_order != step_order or step.status != "PENDING":
        raise ValueError("الخطوة المتزامنة ليست نشطة حاليًا")

    actor_user = User.query.get(actor_user_id)
    eff_user = User.query.get(effective_user_id)
    is_admin = bool(actor_user and (actor_user.has_role("ADMIN") or actor_user.has_role("SUPER_ADMIN"))) or \
        bool(eff_user and (eff_user.has_role("ADMIN") or eff_user.has_role("SUPER_ADMIN")))
    is_previous_step_actor = int(inst.last_step_actor_id or 0) == int(effective_user_id)
    is_committee_chair = can_committee_chair_bypass_parallel_step(effective_user_id, step)

    if not is_admin and not is_previous_step_actor and not is_committee_chair:
        raise PermissionError("غير مخوّل بالتجاوز في هذه الخطوة")

    _ensure_parallel_tasks(req, inst, step)

    pending_tasks = (
        WorkflowStepTask.query
        .filter_by(instance_id=inst.id, step_order=step_order, status="PENDING")
        .order_by(WorkflowStepTask.assignee_user_id.asc())
        .all()
    )
    if is_committee_chair and not is_admin and not is_previous_step_actor:
        pending_tasks = [
            task for task in pending_tasks
            if int(task.assignee_user_id) != int(effective_user_id)
        ]
    if not pending_tasks:
        if is_committee_chair and not is_admin and not is_previous_step_actor:
            raise ValueError("لا يمكن لرئيس اللجنة تجاوز مهمته الشخصية")
        raise ValueError("لا يوجد متزامنون بحالة قيد الانتظار لتجاوزهم")

    now = datetime.utcnow()
    for task in pending_tasks:
        task.status = "BYPASSED"
        task.bypassed_by_id = int(effective_user_id)
        task.bypass_reason = reason[:500]
        task.bypassed_at = now
        db.session.add(task)

    actor_label = (actor_user.full_name if actor_user else f"User#{actor_user_id}")
    eff_label = (eff_user.full_name if eff_user else f"User#{effective_user_id}")
    actor_display = actor_label if not on_behalf_of_id else f"{actor_label} (مفوّض عن {eff_label})"

    db.session.add(
        AuditLog(
            user_id=actor_user_id,
            action="PARALLEL_SYNC_BYPASS_ALL",
            target_type="WORKFLOW_INSTANCE_STEP",
            target_id=step.id,
            note=f"bypass_all step={step_order} count={len(pending_tasks)} by={actor_display} reason={reason[:500]}",
            on_behalf_of_id=on_behalf_of_id,
        )
    )

    # After bypassing all remaining pending tasks, the step MUST be complete.
    if _parallel_total(inst.id, step_order) == 0 or _parallel_is_complete(inst.id, step_order):
        step.status = "APPROVED"
        step.decided_by_id = int(effective_user_id)
        step.decided_at = now
        step.note = "تم إغلاق الخطوة المتزامنة (تجاوز المتبقين)"

        inst.last_step_actor_id = int(effective_user_id)
        next_order = step_order + 1
        next_step = WorkflowInstanceStep.query.filter_by(instance_id=inst.id, step_order=next_order).first()

        if not next_step:
            inst.is_completed = True
            req.status = "APPROVED"
            req.completed_at = now
            db.session.add(
                AuditLog(
                    user_id=actor_user_id,
                    action="WORKFLOW_COMPLETED",
                    target_type="WORKFLOW_REQUEST",
                    target_id=req.id,
                    note=f"Workflow completed after PARALLEL_SYNC step {step_order} by {actor_display}",
                    on_behalf_of_id=on_behalf_of_id,
                )
            )
            _notify_users([req.requester_id], message=f"تم إنجاز الطلب #{req.id} ✅", ntype="WORKFLOW", actor_id=actor_user_id, track_for_actor=True, req=req)
        else:
            inst.current_step_order = next_order
            _activate_step_sla(next_step, started_at=now, reset=True)
            _notify_users([req.requester_id], message=f"اكتملت الخطوة المتزامنة للطلب #{req.id} وتم تحويله للخطوة {next_order} بواسطة {actor_display}", ntype="WORKFLOW", actor_id=actor_user_id, track_for_actor=True, req=req)

            if _is_parallel_sync(next_step):
                _ensure_parallel_tasks(req, inst, next_step)
            else:
                approvers = _resolve_approver_users(next_step)
                _notify_users(
                    approvers,
                    message=f"طلب جديد يحتاج إجراء: #{req.id} (الخطوة {next_order})",
                    ntype="WORKFLOW",
                    role=next_step.approver_role,
                    actor_id=req.requester_id,
                    track_for_actor=True,
                    req=req,
                    task_assignment=True,
                    step_order=next_step.step_order,
                    instance_id=inst.id,
                )

    if auto_commit:
        db.session.commit()

    return inst


def decide_step(
    req_id: int,
    step_order: int,
    actor_user_id: int,
    decision: str,
    note: str = "",
    auto_commit: bool = False,
    effective_user_id: int | None = None,
    on_behalf_of_id: int | None = None,
    delegation_id: int | None = None,
    authorized_parallel_user_ids=None,
    selected_dynamic_branch_step_order=None,
    selected_dynamic_branch_step_orders=None,
):
    """
    Approve/Reject a step.
    decision: APPROVED / REJECTED
    """
    decision = (decision or "").strip().upper()
    if decision not in ("APPROVED", "REJECTED"):
        raise ValueError("قرار غير صالح (يجب أن يكون موافقة أو رفض).")

    req = WorkflowRequest.query.get_or_404(req_id)
    inst = WorkflowInstance.query.filter_by(request_id=req.id).first_or_404()

    step = WorkflowInstanceStep.query.filter_by(
        instance_id=inst.id,
        step_order=step_order
    ).first_or_404()

    if step.status != "PENDING":
        raise ValueError("تم اتخاذ قرار على هذه الخطوة مسبقاً.")

    # Delegation-aware context:
    # - actor_user_id: the logged-in user who performed the action (delegatee)
    # - effective_user_id: the user whose authority is used for approval (delegator)
    effective_user_id = int(effective_user_id or actor_user_id)
    if on_behalf_of_id:
        try:
            on_behalf_of_id = int(on_behalf_of_id)
        except Exception:
            on_behalf_of_id = None
    if delegation_id:
        try:
            delegation_id = int(delegation_id)
        except Exception:
            delegation_id = None

    actor = User.query.get(actor_user_id)
    effective_user = User.query.get(effective_user_id)

    actor_label = (actor.email if actor else f"User#{actor_user_id}")
    eff_label = (effective_user.email if effective_user else f"User#{effective_user_id}")
    # For notifications/messages
    actor_display = actor_label if not on_behalf_of_id else f"{actor_label} (مفوّض عن {eff_label})"

    hierarchy_bypassed_steps: list[WorkflowInstanceStep] = []
    if int(step_order) != int(inst.current_step_order or 0):
        if decision != "APPROVED":
            raise ValueError("يمكن للمستوى الأعلى المتابعة والتجاوز فقط، ولا يمكنه رفض خطوة لم تصل إليه بعد.")
        bypass_target = resolve_hierarchy_bypass_step(inst, [effective_user_id])
        if not bypass_target or int(bypass_target.id) != int(step.id):
            raise ValueError("لا يمكن تنفيذ هذه الخطوة قبل دورها لأنها ليست مستوى أعلى ضمن تسلسل الصعود الحالي.")
        hierarchy_bypassed_steps = _hierarchy_bypass_steps(inst, step)
        if not hierarchy_bypassed_steps:
            raise ValueError("لا توجد خطوات أدنى صالحة للتجاوز ضمن التسلسل الحالي.")

    # -------------------------------------------------
    # PARALLEL_SYNC: responses are for documentation only
    # -------------------------------------------------
    if _is_parallel_sync(step):
        _ensure_parallel_tasks(req, inst, step)

        task = WorkflowStepTask.query.filter_by(
            instance_id=inst.id,
            step_order=step.step_order,
            assignee_user_id=effective_user_id,
        ).first()
        if not task:
            raise ValueError("You are not assigned to this parallel step.")
        if task.status == "BYPASSED":
            raise ValueError("You were bypassed in this step.")
        if task.status == "RESPONDED":
            raise ValueError("You already responded in this step.")

        now = datetime.utcnow()
        task.status = "RESPONDED"
        # For documentation only (does not change routing)
        task.response = "APPROVED" if decision == "APPROVED" else "REJECTED"
        task.responded_at = now
        # Keep the mention marker when a mentioned user responds. It is used
        # to revoke and later reactivate the same assignment safely.
        task.note = (
            "MENTION_TASK" + (f"\n{note}" if note else "")
            if is_mention_task(task)
            else note
        )

        db.session.add(AuditLog(
            request_id=req.id,
            user_id=actor_user_id,
            on_behalf_of_id=on_behalf_of_id,
            delegation_id=delegation_id,
            action="PARALLEL_SYNC_RESPONDED",
            old_status=None,
            new_status=None,
            note=f"الخطوة {step.step_order}: {workflow_status_label(task.response)}. {note}".strip(),
            target_type="PARALLEL_TASK",
            target_id=task.id,
        ))

        # If everyone responded/bypassed => close step and advance.
        if _parallel_total(inst.id, step.step_order) == 0 or _parallel_is_complete(inst.id, step.step_order):
            step.status = "APPROVED"  # completion marker
            step.decided_by_id = effective_user_id
            step.decided_at = now
            step.note = (note or "").strip()

            # who executed this (closing) action becomes the previous-step actor for the next step
            inst.last_step_actor_id = effective_user_id

            next_order = step.step_order + 1
            next_step = WorkflowInstanceStep.query.filter_by(
                instance_id=inst.id,
                step_order=next_order
            ).first()

            # notify requester about parallel completion
            if req.requester_id:
                _notify_users([req.requester_id], f"اكتملت الخطوة المتزامنة للطلب #{req.id}.", ntype="WORKFLOW", req=req)

            if not next_step:
                # complete workflow
                req.status = "APPROVED"
                inst.is_completed = True
                inst.current_step_order = next_order
                db.session.add(AuditLog(
                    request_id=req.id,
                    user_id=actor_user_id,
                    on_behalf_of_id=on_behalf_of_id,
                    delegation_id=delegation_id,
                    action="WORKFLOW_COMPLETED",
                    old_status="IN_PROGRESS",
                    new_status=req.status,
                    note=f"Completed after PARALLEL_SYNC step {step.step_order}.",
                    target_type="WORKFLOW",
                    target_id=inst.id,
                ))
            else:
                inst.current_step_order = next_order
                _activate_step_sla(next_step, started_at=now, reset=True)
                # ensure tasks if the next step is also parallel, otherwise notify approvers
                if _is_parallel_sync(next_step):
                    _ensure_parallel_tasks(req, inst, next_step)
                else:
                    approvers = _resolve_approver_users(next_step)
                    _notify_users(
                        approvers,
                        message=f"طلب جديد يحتاج إجراء: #{req.id} (الخطوة {next_order})",
                        ntype="WORKFLOW",
                        role=next_step.approver_role,
                        actor_id=actor_user_id,
                        track_for_actor=True,
                        req=req,
                        task_assignment=True,
                        step_order=next_step.step_order,
                        instance_id=inst.id,
                    )

        if auto_commit:
            db.session.commit()
        return

    # -------------------------------------------------
    # SEQUENTIAL: approval decision drives routing
    # -------------------------------------------------
    next_step_for_authorization = None
    dynamic_branch_steps: list[WorkflowInstanceStep] = []
    selected_dynamic_branch_steps: list[WorkflowInstanceStep] = []
    if decision == "APPROVED":
        dynamic_branch_steps = resolve_dynamic_branch_steps(inst, step)
        if dynamic_branch_steps:
            raw_selected_orders = selected_dynamic_branch_step_orders
            if raw_selected_orders is None:
                raw_selected_orders = (
                    [selected_dynamic_branch_step_order]
                    if selected_dynamic_branch_step_order is not None
                    else []
                )
            elif isinstance(raw_selected_orders, (str, int)):
                raw_selected_orders = [raw_selected_orders]
            try:
                selected_branch_orders = {
                    int(branch_order)
                    for branch_order in raw_selected_orders
                    if str(branch_order).strip()
                }
            except (TypeError, ValueError):
                raise ValueError("اختر دائرة واحدة على الأقل لتوجيه المسار إليها")
            if not selected_branch_orders:
                raise ValueError("اختر دائرة واحدة على الأقل لتوجيه المسار إليها")

            available_branch_orders = {
                int(branch_step.step_order) for branch_step in dynamic_branch_steps
            }
            if not selected_branch_orders.issubset(available_branch_orders):
                raise ValueError("إحدى الدوائر المختارة ليست ضمن فروع المسار المتاحة")
            selected_dynamic_branch_steps = [
                branch_step
                for branch_step in dynamic_branch_steps
                if int(branch_step.step_order) in selected_branch_orders
            ]

        next_step_for_authorization = WorkflowInstanceStep.query.filter_by(
            instance_id=inst.id,
            step_order=step_order + 1,
        ).first()
        if next_step_for_authorization and _is_parallel_sync(next_step_for_authorization):
            candidate_ids = set(resolve_parallel_candidate_user_ids(
                req,
                inst,
                next_step_for_authorization,
            ))
            try:
                selected_ids = {
                    int(uid) for uid in (authorized_parallel_user_ids or []) if int(uid) > 0
                }
            except (TypeError, ValueError):
                raise ValueError("قائمة المخولين في الخطوة المتزامنة غير صالحة")
            if not selected_ids:
                raise ValueError("اختر شخصًا واحدًا على الأقل لتوجيه الخطوة المتزامنة التالية")
            if not selected_ids.issubset(candidate_ids):
                raise ValueError("يمكن توجيه الخطوة المتزامنة فقط إلى المرشحين المحددين في القالب")

    if hierarchy_bypassed_steps:
        bypassed_at = datetime.utcnow()
        follower_ids: set[int] = set()
        for bypassed_step in hierarchy_bypassed_steps:
            follower_ids.update(resolve_step_approver_user_ids(bypassed_step))
            bypassed_step.status = "SKIPPED"
            bypassed_step.decided_by_id = effective_user_id
            bypassed_step.decided_at = bypassed_at
            bypassed_step.note = (
                f"تم تجاوز هذه الخطوة هرمياً بواسطة {actor_display}؛ "
                "وبقي المسؤول عنها ضمن متابعي الطلب."
            )
            db.session.add(bypassed_step)
            db.session.add(AuditLog(
                request_id=req.id,
                user_id=actor_user_id,
                on_behalf_of_id=on_behalf_of_id,
                delegation_id=delegation_id,
                action="HIERARCHY_STEP_BYPASSED",
                old_status="PENDING",
                new_status="SKIPPED",
                note=(
                    f"تم تجاوز الخطوة {bypassed_step.step_order} بواسطة المستوى الأعلى "
                    f"في الخطوة {step_order}."
                ),
                target_type="WORKFLOW_STEP",
                target_id=bypassed_step.id,
            ))

        follower_ids.discard(int(effective_user_id))
        follower_ids.discard(int(req.requester_id or 0))
        for follower_id in sorted(follower_ids):
            existing_follower = (
                AuditLog.query
                .filter_by(
                    request_id=req.id,
                    action=HIERARCHY_BYPASS_FOLLOWER_ACTION,
                    target_type="USER",
                    target_id=follower_id,
                )
                .first()
            )
            if not existing_follower:
                db.session.add(AuditLog(
                    request_id=req.id,
                    user_id=actor_user_id,
                    on_behalf_of_id=on_behalf_of_id,
                    delegation_id=delegation_id,
                    action=HIERARCHY_BYPASS_FOLLOWER_ACTION,
                    old_status=None,
                    new_status=None,
                    note=(
                        f"إبقاء المستخدم #{follower_id} ضمن المتابعين بعد تجاوز "
                        f"الخطوات الأدنى وصولاً إلى الخطوة {step_order}."
                    ),
                    target_type="USER",
                    target_id=follower_id,
                ))

        if follower_ids:
            _notify_users(
                sorted(follower_ids),
                message=(
                    f"تمت متابعة الطلب #{req.id} من مستوى إداري أعلى بواسطة {actor_display}. "
                    "تم تجاوز انتظار خطوتك، وستبقى مطلعاً على جميع التحديثات اللاحقة."
                ),
                ntype="WORKFLOW",
                actor_id=actor_user_id,
                track_for_actor=True,
                req=req,
            )

        db.session.add(AuditLog(
            request_id=req.id,
            user_id=actor_user_id,
            on_behalf_of_id=on_behalf_of_id,
            delegation_id=delegation_id,
            action="HIERARCHY_BYPASS_EXECUTED",
            old_status=None,
            new_status=None,
            note=(
                f"تم الانتقال من الخطوة {inst.current_step_order} إلى المستوى الأعلى "
                f"في الخطوة {step_order} وتجاوز {len(hierarchy_bypassed_steps)} خطوة/خطوات."
            ),
            target_type="WORKFLOW_STEP",
            target_id=step.id,
        ))

    # Track who executed this step (effective user). This is used as bypass authority
    # when the *next* step is PARALLEL_SYNC.
    inst.last_step_actor_id = effective_user_id

    step.status = decision
    # Credit the decision to the effective user (delegator) for workflow history/following.
    step.decided_by_id = effective_user_id
    step.decided_at = datetime.utcnow()
    step.note = note
    db.session.add(step)

    db.session.add(
        AuditLog(
            request_id=req.id,
            user_id=actor_user_id,
            on_behalf_of_id=on_behalf_of_id,
            delegation_id=delegation_id,
            action=f"STEP_{decision}",
            old_status=None,
            new_status=None,
            note=f"الخطوة {step_order}: {note}".strip(),
            target_type="WORKFLOW_STEP",
            target_id=step.id,
        )
    )

    if decision == "REJECTED":
        req.status = "REJECTED"
        inst.is_completed = True
        db.session.add_all([req, inst])

        # Notify requester with the decision + note
        msg = f"تم رفض طلبك #{req.id} (الخطوة {step_order}) من {actor_display}"
        if note:
            msg += f" | السبب/التعليق: {note}"
        _notify_users([req.requester_id], message=msg, ntype="WORKFLOW", actor_id=actor_user_id, track_for_actor=True, req=req)

        # ✅ Notify followers (previous approvers) so they can keep tracking the workflow
        follower_ids = set(_resolve_followers_user_ids(inst.id))
        follower_ids.discard(int(effective_user_id))
        follower_ids.discard(int(req.requester_id))
        if follower_ids:
            fmsg = f"تحديث على المسار: تم رفض الطلب #{req.id} (الخطوة {step_order}) من {actor_display}"
            if note:
                fmsg += f" | السبب/التعليق: {note}"
            _notify_users(sorted(follower_ids), message=fmsg, ntype="WORKFLOW", req=req)

        if auto_commit:
            db.session.commit()
        return

    if selected_dynamic_branch_steps:
        branch_decided_at = datetime.utcnow()
        selected_branch_orders = {
            int(branch_step.step_order) for branch_step in selected_dynamic_branch_steps
        }
        skipped_labels: list[str] = []
        skipped_branch_node_ids: set[int] = set()
        for branch_step in dynamic_branch_steps:
            if int(branch_step.step_order) in selected_branch_orders:
                continue
            branch_step.status = "SKIPPED"
            branch_step.decided_by_id = effective_user_id
            branch_step.decided_at = branch_decided_at
            branch_step.note = (
                f"تم استبعاد هذا الفرع عند توجيه المسار من الخطوة {step_order}."
            )
            skipped_labels.append(
                branch_step.routing_label or f"الخطوة {branch_step.step_order}"
            )
            if branch_step.approver_org_node_id:
                skipped_branch_node_ids.add(int(branch_step.approver_org_node_id))
            db.session.add(branch_step)

        # The return leg mirrors the selected forward branches.  If a sibling
        # branch was excluded at the routing point, exclude its mirrored return
        # step as well so the request cannot re-enter that branch on the way back.
        if skipped_branch_node_ids:
            return_steps = (
                WorkflowInstanceStep.query
                .filter(
                    WorkflowInstanceStep.instance_id == inst.id,
                    WorkflowInstanceStep.step_order > max(
                        int(branch_step.step_order) for branch_step in dynamic_branch_steps
                    ),
                    WorkflowInstanceStep.status == "PENDING",
                    WorkflowInstanceStep.routing_reason == DYNAMIC_RETURN_REASON,
                    WorkflowInstanceStep.approver_kind == "ORG_NODE",
                    WorkflowInstanceStep.approver_org_node_id.in_(skipped_branch_node_ids),
                )
                .all()
            )
            for return_step in return_steps:
                return_step.status = "SKIPPED"
                return_step.decided_by_id = effective_user_id
                return_step.decided_at = branch_decided_at
                return_step.note = "تم استبعاد فرع العودة تبعاً لاستبعاد الفرع في مسار الذهاب."
                db.session.add(return_step)

        selected_labels = [
            branch_step.routing_label or f"الخطوة {branch_step.step_order}"
            for branch_step in selected_dynamic_branch_steps
        ]
        db.session.add(AuditLog(
            request_id=req.id,
            user_id=actor_user_id,
            on_behalf_of_id=on_behalf_of_id,
            delegation_id=delegation_id,
            action="DYNAMIC_BRANCH_SELECTED",
            old_status=None,
            new_status=None,
            note=(
                f"تم توجيه المسار إلى: {', '.join(selected_labels)}"
                + (f" واستبعاد: {', '.join(skipped_labels)}" if skipped_labels else "")
            ),
            target_type="WORKFLOW_INSTANCE_STEP",
            target_id=selected_dynamic_branch_steps[0].id,
        ))

    # move to next step
    if selected_dynamic_branch_steps:
        next_step = selected_dynamic_branch_steps[0]
    else:
        next_step = (
            WorkflowInstanceStep.query
            .filter(
                WorkflowInstanceStep.instance_id == inst.id,
                WorkflowInstanceStep.step_order > int(step_order),
                WorkflowInstanceStep.status == "PENDING",
            )
            .order_by(WorkflowInstanceStep.step_order.asc())
            .first()
        )
    next_order = int(next_step.step_order) if next_step else int(step_order) + 1

    if not next_step:
        req.status = "APPROVED"
        inst.is_completed = True
        db.session.add_all([req, inst])

        msg = f"تمت متابعة طلبك #{req.id} حتى اكتمال المسار بواسطة {actor_display}"
        if note:
            msg += f" | ملاحظة: {note}"
        _notify_users([req.requester_id], message=msg, ntype="WORKFLOW", actor_id=actor_user_id, track_for_actor=True, req=req)

        # ✅ Notify followers (previous approvers)
        follower_ids = set(_resolve_followers_user_ids(inst.id))
        follower_ids.discard(int(effective_user_id))
        follower_ids.discard(int(req.requester_id))
        if follower_ids:
            _notify_users(
                sorted(follower_ids),
                message=f"تحديث على المسار: تمت متابعة الطلب #{req.id} حتى اكتمال المسار بواسطة {actor_display}" + (f" | ملاحظة: {note}" if note else ""),
                ntype="WORKFLOW",
                req=req,
            )

        if auto_commit:
            db.session.commit()
        return

    inst.current_step_order = next_order
    _activate_step_sla(next_step, started_at=step.decided_at, reset=True)
    db.session.add(inst)

    msg = f"تمت متابعة طلبك #{req.id} (الخطوة {step_order}) بواسطة {actor_display} وتم تحويله للخطوة {next_order}"
    if note:
        msg += f" | ملاحظة: {note}"
    _notify_users([req.requester_id], message=msg, ntype="WORKFLOW", actor_id=actor_user_id, track_for_actor=True, req=req)

    if _is_parallel_sync(next_step):
        authorize_parallel_step(
            req,
            inst,
            next_step,
            authorized_user_ids=authorized_parallel_user_ids,
            actor_user_id=actor_user_id,
            effective_user_id=effective_user_id,
            on_behalf_of_id=on_behalf_of_id,
            delegation_id=delegation_id,
            auto_commit=False,
        )
    else:
        approvers = _resolve_approver_users(next_step)
        _notify_users(
            approvers,
            message=f"طلب جديد يحتاج إجراء: #{req.id} (الخطوة {next_order})",
            ntype="WORKFLOW",
            role=next_step.approver_role,
            actor_id=req.requester_id,
            track_for_actor=True,
            req=req,
            task_assignment=True,
            step_order=next_step.step_order,
            instance_id=inst.id,
        )

    # ✅ Notify followers (previous approvers)
    follower_ids = set(_resolve_followers_user_ids(inst.id))
    follower_ids.discard(int(effective_user_id))
    follower_ids.discard(int(req.requester_id))
    if follower_ids:
        _notify_users(
            sorted(follower_ids),
            message=f"تحديث على المسار: تمت الموافقة على الطلب #{req.id} (الخطوة {step_order}) وتحويله للخطوة {next_order} من {actor_display}" + (f" | ملاحظة: {note}" if note else ""),
            ntype="WORKFLOW",
            req=req,
        )

    if auto_commit:
        db.session.commit()
