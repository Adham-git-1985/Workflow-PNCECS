# workflow/engine.py

from datetime import datetime, timedelta
import uuid

from extensions import db
from models import (
    AuditLog,
    Notification,
    User,
    WorkflowRequest,
    WorkflowTemplate,
    WorkflowTemplateStep,
    WorkflowInstance,
    WorkflowInstanceStep,
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
def _resolve_approver_users(step: WorkflowInstanceStep):
    """
    Returns list of user_ids to notify.
    - USER: notify that user
    - DEPARTMENT: notify dept_head(s)
    - ROLE: notify all users with that role
    """
    kind = (step.approver_kind or "").upper()

    if kind == "USER" and step.approver_user_id:
        return [step.approver_user_id]

    if kind == "ROLE" and step.approver_role:
        role = (step.approver_role or "").strip().lower()
        users = User.query.filter(User.role.ilike(role)).all()
        return [u.id for u in users]

    if kind == "DEPARTMENT" and step.approver_department_id:
        users = User.query.filter(
            User.department_id == step.approver_department_id,
            User.role.ilike("dept_head")
        ).all()
        return [u.id for u in users]

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
    db.session.add(inst)
    db.session.flush()  # get inst.id

    template_sla = template.sla_days_default

    # IMPORTANT: in your models, template.steps is a LIST (not dynamic query)
    # and already ordered by step_order (relationship order_by).
    tsteps = list(template.steps or [])

    for ts in tsteps:
        step = WorkflowInstanceStep(
            instance_id=inst.id,
            step_order=ts.step_order,
            approver_kind=ts.approver_kind,
            approver_user_id=ts.approver_user_id,
            approver_department_id=ts.approver_department_id,
            approver_role=ts.approver_role,
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
        note=f"Template: {template.name}",
        target_type="WORKFLOW",
        target_id=inst.id
    ))

    # notify first step approvers
    first = WorkflowInstanceStep.query.filter_by(
        instance_id=inst.id,
        step_order=1
    ).first()

    if first:
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
    return inst


def decide_step(req_id: int, step_order: int, actor_user_id: int, decision: str, note: str = "", auto_commit: bool = False):
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

    actor = User.query.get(actor_user_id)
    actor_label = actor.email if actor else f"User#{actor_user_id}"

    step.status = decision
    step.decided_by_id = actor_user_id
    step.decided_at = datetime.utcnow()
    step.note = note
    db.session.add(step)

    db.session.add(AuditLog(
        request_id=req.id,
        user_id=actor_user_id,
        action=f"STEP_{decision}",
        old_status=None,
        new_status=None,
        note=f"Step {step_order}: {note}".strip(),
        target_type="WORKFLOW_STEP",
        target_id=step.id
    ))

    if decision == "REJECTED":
        req.status = "REJECTED"
        inst.is_completed = True
        db.session.add_all([req, inst])

        # Notify requester with the decision + note
        msg = f"تم رفض طلبك #{req.id} (الخطوة {step_order}) من {actor_label}"
        if note:
            msg += f" | السبب/التعليق: {note}"
        _notify_users([req.requester_id], message=msg, ntype="WORKFLOW", actor_id=actor_user_id, track_for_actor=True)

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

        msg = f"تمت الموافقة النهائية على طلبك #{req.id} من {actor_label}"
        if note:
            msg += f" | ملاحظة: {note}"
        _notify_users([req.requester_id], message=msg, ntype="WORKFLOW", actor_id=actor_user_id, track_for_actor=True)

        if auto_commit:
            db.session.commit()
        return

    inst.current_step_order = next_order
    db.session.add(inst)

    msg = f"تمت الموافقة على طلبك #{req.id} (الخطوة {step_order}) من {actor_label} وتم تحويله للخطوة {next_order}"
    if note:
        msg += f" | ملاحظة: {note}"
    _notify_users([req.requester_id], message=msg, ntype="WORKFLOW", actor_id=actor_user_id, track_for_actor=True)

    approvers = _resolve_approver_users(next_step)
    _notify_users(
        approvers,
        message=f"طلب جديد يحتاج إجراء: #{req.id} (الخطوة {next_order})",
        ntype="WORKFLOW",
        role=next_step.approver_role,
        actor_id=req.requester_id,
        track_for_actor=True
    )

    if auto_commit:
        db.session.commit()