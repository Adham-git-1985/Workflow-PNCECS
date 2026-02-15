# workflow/engine.py

from datetime import datetime, timedelta
import uuid

from sqlalchemy.exc import IntegrityError

from extensions import db
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
    OrgNodeManager,
)

# =========================
# SLA helpers
# =========================
def _system_sla_days():
    # إذا عندك SystemSetting لاحقًا اربطها هنا
    return 3


def _step_due_at(template_sla_days, step_sla_days):
    days = step_sla_days or template_sla_days or _system_sla_days()
    return datetime.utcnow() + timedelta(days=int(days))


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
        # Prefer OrgUnitManager if configured
        ids = _resolve_org_manager_ids('DIRECTORATE', did)
        if ids:
            return ids
        # Fallback: role-based (legacy)
        users = (
            User.query.join(Department, User.department_id == Department.id)
            .filter(Department.directorate_id == did, User.role.ilike('directorate_head'))
            .all()
        )
        return [int(u.id) for u in users]

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
        rows = WorkflowTemplateParallelAssignee.query.filter_by(template_step_id=ts.id).all()
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


def _notify_users(user_ids, message, ntype="WORKFLOW", role=None, actor_id=None, track_for_actor=False):
    """
    Your Notification model has: message, type, role, is_read, created_at
    (no title/url) => keep it compatible.
    """
    if not user_ids:
        return

    now = datetime.utcnow()
    event_key = uuid.uuid4().hex
    unique_ids = set(int(uid) for uid in user_ids if uid)

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
            )
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
            )
        )


# =========================
# Parallel Sync helpers
# =========================

def _is_parallel_sync(step: WorkflowInstanceStep) -> bool:
    return (getattr(step, "mode", "SEQUENTIAL") or "SEQUENTIAL") == "PARALLEL_SYNC"


def _ensure_parallel_tasks(req: WorkflowRequest, inst: WorkflowInstance, step: WorkflowInstanceStep):
    """Create per-assignee tasks for a PARALLEL_SYNC step (idempotent)."""
    if not _is_parallel_sync(step):
        return

    # 1) Ensure tasks exist (idempotent)
    existing_count = (
        WorkflowStepTask.query
        .filter_by(instance_id=inst.id, step_order=step.step_order)
        .count()
    )

    assignees: list[int] = []
    now = datetime.utcnow()

    if existing_count == 0:
        assignees = _resolve_approver_users(step)
        # Add extra assignees linked to this PARALLEL_SYNC step number (template definition)
        assignees += _resolve_parallel_extra_assignees(getattr(inst, "template_id", None), step.step_order)
        assignees = sorted({int(uid) for uid in assignees if uid})

        # Use a SAVEPOINT so a unique-constraint race won't rollback the whole outer transaction.
        try:
            with db.session.begin_nested():
                for uid in assignees:
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
        except IntegrityError:
            # Someone else already created the tasks; do not notify again.
            db.session.expire_all()
            return
    else:
        # Tasks already exist; we can derive assignees from tasks if needed.
        assignees = [
            int(uid) for (uid,) in (
                db.session.query(WorkflowStepTask.assignee_user_id)
                .filter_by(instance_id=inst.id, step_order=step.step_order)
                .all()
            )
            if uid
        ]

    # 2) Notify all assignees ONCE per step activation
    if assignees and not getattr(step, "parallel_notified_at", None):
        _notify_users(
            assignees,
            f"مهمة متزامنة للطلب #{req.id}: يرجى الرد (للتوثيق فقط).",
            ntype="WORKFLOW",
        )
        step.parallel_notified_at = now
        db.session.add(step)


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
    """Users who already decided on at least one step in this instance (followers)."""
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
    return sorted(ids)


# =========================
# Engine API
# =========================
def start_workflow_for_request(
    req: WorkflowRequest,
    template: WorkflowTemplate,
    created_by_user_id: int,
    auto_commit: bool = False
):
    """
    Creates workflow instance + instance steps from template.
    Sets first step due_at, and notifies approvers.
    """
    inst = WorkflowInstance(
        request_id=req.id,
        template_id=template.id,
        current_step_order=1
    )
    # For PARALLEL_SYNC: the request creator is considered the "previous step actor"
    # for the first step (thus can bypass if step 1 is parallel).
    inst.last_step_actor_id = int(created_by_user_id) if created_by_user_id else None
    db.session.add(inst)
    db.session.flush()  # get inst.id

    template_sla = template.sla_days_default

    # IMPORTANT: in your models, template.steps is a LIST (not dynamic query)
    # and already ordered by step_order (relationship order_by).
    tsteps = list(template.steps or [])

    for ts in tsteps:
        # Defensive normalization (some seeded/legacy data may store lowercase/extra spaces)
        _kind = ((getattr(ts, 'approver_kind', None) or '').strip().upper())
        if _kind not in ("USER", "ROLE", "DEPARTMENT", "DIRECTORATE", "UNIT", "SECTION", "DIVISION", "COMMITTEE"):
            _kind = ""

        _cmode = getattr(ts, 'committee_delivery_mode', None)
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

        step = WorkflowInstanceStep(
            instance_id=inst.id,
            step_order=ts.step_order,
            mode=getattr(ts, "mode", "SEQUENTIAL") or "SEQUENTIAL",
            approver_kind=_kind,
            approver_user_id=ts.approver_user_id,
            approver_department_id=ts.approver_department_id,
            approver_directorate_id=getattr(ts, 'approver_directorate_id', None),
            approver_unit_id=getattr(ts, 'approver_unit_id', None),
            approver_section_id=getattr(ts, 'approver_section_id', None),
            approver_division_id=getattr(ts, 'approver_division_id', None),
            approver_role=ts.approver_role,
            approver_committee_id=getattr(ts, 'approver_committee_id', None),
            committee_delivery_mode=_cmode,
            status="PENDING",
            due_at=_step_due_at(template_sla, ts.sla_days),
        )
        db.session.add(step)

    req.status = "IN_PROGRESS"
    db.session.add(req)
    db.session.flush()

    db.session.add(AuditLog(
        request_id=req.id,
        user_id=created_by_user_id,
        action="WORKFLOW_STARTED",
        old_status=None,
        new_status=req.status,
        note=f"Template: {template.name} (#{template.id})",
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
            _ensure_parallel_tasks(req, inst, first)
        else:
            approvers = _resolve_approver_users(first)
            _notify_users(
                approvers,
                message=f"طلب جديد يحتاج إجراء: #{req.id} (الخطوة 1)",
                ntype="WORKFLOW",
                role=first.approver_role,
                actor_id=req.requester_id,
                track_for_actor=True
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
            )
        else:
            inst.current_step_order = next_order
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
                    track_for_actor=True
                )

            _notify_users(
                [req.requester_id],
                message=f"اكتملت الخطوة المتزامنة للطلب #{req.id} وتم تحويله للخطوة {next_order}.",
                ntype="WORKFLOW",
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

    # Authorization: admin/superadmin OR previous-step actor (effective; delegation-aware)
    actor_user = User.query.get(actor_user_id)
    eff_user = User.query.get(effective_user_id)

    is_admin = bool(actor_user and (actor_user.has_role("ADMIN") or actor_user.has_role("SUPER_ADMIN"))) or \
        bool(eff_user and (eff_user.has_role("ADMIN") or eff_user.has_role("SUPER_ADMIN")))

    if not is_admin and int(inst.last_step_actor_id or 0) != int(effective_user_id):
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
        raise ValueError("لا يمكن تجاوز مستخدم حالته ليست PENDING")

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
            _notify_users([req.requester_id], message=f"تم إنجاز الطلب #{req.id} ✅", ntype="WORKFLOW", actor_id=actor_user_id, track_for_actor=True)
        else:
            inst.current_step_order = next_order
            msg = f"اكتملت الخطوة المتزامنة للطلب #{req.id} وتم تحويله للخطوة {next_order} بواسطة {actor_display}"
            _notify_users([req.requester_id], message=msg, ntype="WORKFLOW", actor_id=actor_user_id, track_for_actor=True)

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

    if not is_admin and int(inst.last_step_actor_id or 0) != int(effective_user_id):
        raise PermissionError("غير مخوّل بالتجاوز في هذه الخطوة")

    _ensure_parallel_tasks(req, inst, step)

    pending_tasks = (
        WorkflowStepTask.query
        .filter_by(instance_id=inst.id, step_order=step_order, status="PENDING")
        .order_by(WorkflowStepTask.assignee_user_id.asc())
        .all()
    )
    if not pending_tasks:
        raise ValueError("لا يوجد متزامنون بحالة PENDING لتجاوزهم")

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
            _notify_users([req.requester_id], message=f"تم إنجاز الطلب #{req.id} ✅", ntype="WORKFLOW", actor_id=actor_user_id, track_for_actor=True)
        else:
            inst.current_step_order = next_order
            _notify_users([req.requester_id], message=f"اكتملت الخطوة المتزامنة للطلب #{req.id} وتم تحويله للخطوة {next_order} بواسطة {actor_display}", ntype="WORKFLOW", actor_id=actor_user_id, track_for_actor=True)

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
):
    """
    Approve/Reject a step.
    decision: APPROVED / REJECTED
    """
    decision = (decision or "").strip().upper()
    if decision not in ("APPROVED", "REJECTED"):
        raise ValueError("Invalid decision (must be APPROVED or REJECTED).")

    req = WorkflowRequest.query.get_or_404(req_id)
    inst = WorkflowInstance.query.filter_by(request_id=req.id).first_or_404()

    step = WorkflowInstanceStep.query.filter_by(
        instance_id=inst.id,
        step_order=step_order
    ).first_or_404()

    if step.status != "PENDING":
        raise ValueError("Step already decided.")

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
        task.note = note

        db.session.add(AuditLog(
            request_id=req.id,
            user_id=actor_user_id,
            on_behalf_of_id=on_behalf_of_id,
            delegation_id=delegation_id,
            action="PARALLEL_SYNC_RESPONDED",
            old_status=None,
            new_status=None,
            note=f"Step {step.step_order}: {task.response}. {note}".strip(),
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
                _notify_users([req.requester_id], f"اكتملت الخطوة المتزامنة للطلب #{req.id}.", ntype="WORKFLOW")

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
                        track_for_actor=True
                    )

        if auto_commit:
            db.session.commit()
        return

    # -------------------------------------------------
    # SEQUENTIAL: approval decision drives routing
    # -------------------------------------------------
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
            note=f"Step {step_order}: {note}".strip(),
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
        _notify_users([req.requester_id], message=msg, ntype="WORKFLOW", actor_id=actor_user_id, track_for_actor=True)

        # ✅ Notify followers (previous approvers) so they can keep tracking the workflow
        follower_ids = set(_resolve_followers_user_ids(inst.id))
        follower_ids.discard(int(effective_user_id))
        follower_ids.discard(int(req.requester_id))
        if follower_ids:
            fmsg = f"تحديث على المسار: تم رفض الطلب #{req.id} (الخطوة {step_order}) من {actor_display}"
            if note:
                fmsg += f" | السبب/التعليق: {note}"
            _notify_users(sorted(follower_ids), message=fmsg, ntype="WORKFLOW")

        if auto_commit:
            db.session.commit()
        return

    # move to next step
    next_order = step_order + 1
    next_step = WorkflowInstanceStep.query.filter_by(
        instance_id=inst.id,
        step_order=next_order
    ).first()

    if not next_step:
        req.status = "APPROVED"
        inst.is_completed = True
        db.session.add_all([req, inst])

        msg = f"تمت الموافقة النهائية على طلبك #{req.id} من {actor_display}"
        if note:
            msg += f" | ملاحظة: {note}"
        _notify_users([req.requester_id], message=msg, ntype="WORKFLOW", actor_id=actor_user_id, track_for_actor=True)

        # ✅ Notify followers (previous approvers)
        follower_ids = set(_resolve_followers_user_ids(inst.id))
        follower_ids.discard(int(effective_user_id))
        follower_ids.discard(int(req.requester_id))
        if follower_ids:
            _notify_users(
                sorted(follower_ids),
                message=f"تحديث على المسار: تمت الموافقة النهائية على الطلب #{req.id} من {actor_display}" + (f" | ملاحظة: {note}" if note else ""),
                ntype="WORKFLOW",
            )

        if auto_commit:
            db.session.commit()
        return

    inst.current_step_order = next_order
    db.session.add(inst)

    msg = f"تمت الموافقة على طلبك #{req.id} (الخطوة {step_order}) من {actor_display} وتم تحويله للخطوة {next_order}"
    if note:
        msg += f" | ملاحظة: {note}"
    _notify_users([req.requester_id], message=msg, ntype="WORKFLOW", actor_id=actor_user_id, track_for_actor=True)

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
        )

    if auto_commit:
        db.session.commit()