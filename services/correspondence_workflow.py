"""Bridge correspondence records and workflow requests.

The correspondence module owns the official inbound/outbound record while
Workflow owns routing and approvals.  This module keeps both sides linked
without importing either blueprint (which would create circular imports).
"""

from __future__ import annotations

from datetime import datetime

from extensions import db
from models import (
    CorrMovement,
    Department,
    Directorate,
    Division,
    InboundMail,
    OrgNode,
    Organization,
    OutboundMail,
    Section,
    Unit,
    User,
    WorkflowInstance,
    WorkflowInstanceStep,
    WorkflowRequest,
)


def source_correspondence(req: WorkflowRequest):
    """Return ``(kind, item)`` for a correspondence-backed workflow."""
    source_id = getattr(req, "source_corr_id", None)
    source_kind = (getattr(req, "source_corr_kind", None) or "").strip().upper()
    if not source_id:
        return None, None
    if source_kind == "IN":
        return "IN", db.session.get(InboundMail, int(source_id))
    if source_kind == "OUT":
        return "OUT", db.session.get(OutboundMail, int(source_id))
    return None, None


def correspondence_context(req: WorkflowRequest) -> dict | None:
    """Build the source card displayed on a Workflow request page."""
    kind, item = source_correspondence(req)
    if not item:
        return None

    is_inbound = kind == "IN"
    movements = (
        CorrMovement.query
        .filter_by(
            inbound_id=item.id if is_inbound else None,
            outbound_id=item.id if not is_inbound else None,
        )
        .order_by(CorrMovement.created_at.desc(), CorrMovement.id.desc())
        .limit(8)
        .all()
    )
    return {
        "kind": kind,
        "kind_label": "وارد" if is_inbound else "صادر",
        "item": item,
        "ref_no": item.ref_no or item.id,
        "date": item.received_date if is_inbound else item.sent_date,
        "party_label": "الجهة المرسلة" if is_inbound else "الجهة المستلمة",
        "party": item.sender if is_inbound else item.recipient,
        "status": (item.status or ("RECEIVED" if is_inbound else "DRAFT")).upper(),
        "subject": item.subject or "",
        "body": item.body or "",
        "category": item.category or "",
        "competence": item.competence_label or item.current_target_label or "",
        "target": item.current_target_label or item.competence_label or "",
        "priority": item.priority or "NORMAL",
        "confidentiality": item.confidentiality or "NORMAL",
        "due_date": item.due_date,
        "movements": movements,
        "source_inbound_id": getattr(item, "source_inbound_id", None),
    }


def _target_from_step(step: WorkflowInstanceStep | None) -> dict:
    if not step:
        return {"kind": None, "id": None, "label": None, "user_id": None}

    kind = (getattr(step, "approver_kind", None) or "").strip().upper()
    model_and_id = {
        "USER": (User, getattr(step, "approver_user_id", None)),
        "ORGANIZATION": (Organization, getattr(step, "approver_organization_id", None)),
        "DIRECTORATE": (Directorate, getattr(step, "approver_directorate_id", None)),
        "UNIT": (Unit, getattr(step, "approver_unit_id", None)),
        "DEPARTMENT": (Department, getattr(step, "approver_department_id", None)),
        "SECTION": (Section, getattr(step, "approver_section_id", None)),
        "DIVISION": (Division, getattr(step, "approver_division_id", None)),
        "ORG_NODE": (OrgNode, getattr(step, "approver_org_node_id", None)),
    }.get(kind)

    target_id = None
    label = None
    if model_and_id:
        model, target_id = model_and_id
        if target_id:
            row = db.session.get(model, int(target_id))
            if row:
                label = (
                    getattr(row, "full_name", None)
                    or getattr(row, "name_ar", None)
                    or getattr(row, "name", None)
                    or getattr(row, "email", None)
                )
    elif kind == "ROLE":
        label = getattr(step, "approver_role", None)

    if not label and kind:
        label = kind
    return {
        "kind": kind or None,
        "id": int(target_id) if target_id else None,
        "label": label,
        "user_id": int(target_id) if kind == "USER" and target_id else None,
    }


def _workflow_target(req: WorkflowRequest) -> dict:
    inst = WorkflowInstance.query.filter_by(request_id=req.id).first()
    if not inst or getattr(inst, "is_completed", False):
        return {"kind": None, "id": None, "label": None, "user_id": None}
    step = WorkflowInstanceStep.query.filter_by(
        instance_id=inst.id,
        step_order=inst.current_step_order,
    ).first()
    return _target_from_step(step)


def _mapped_status(req_status: str | None, current_status: str | None) -> str:
    status = (req_status or "").strip().upper()
    if status == "IN_PROGRESS":
        return "IN_PROGRESS"
    if status == "APPROVED":
        return "APPROVED"
    if status == "REJECTED":
        return "RETURNED"
    return (current_status or "RECEIVED").strip().upper()


def _movement_action(req_status: str | None) -> str:
    return {
        "IN_PROGRESS": "WORKFLOW_SYNC",
        "APPROVED": "WORKFLOW_APPROVED",
        "REJECTED": "WORKFLOW_REJECTED",
    }.get((req_status or "").strip().upper(), "WORKFLOW_SYNC")


def _append_movement(
    *,
    kind: str,
    item,
    actor_user_id: int,
    action: str,
    old_status: str | None,
    new_status: str | None,
    target: dict,
    note: str | None,
) -> None:
    db.session.add(CorrMovement(
        inbound_id=item.id if kind == "IN" else None,
        outbound_id=item.id if kind == "OUT" else None,
        actor_user_id=int(actor_user_id),
        action=action,
        from_status=old_status,
        to_status=new_status,
        target_kind=target.get("kind"),
        target_id=target.get("id"),
        target_label=target.get("label"),
        target_user_id=target.get("user_id"),
        note=(note or "").strip() or None,
        is_internal=False,
        created_at=datetime.utcnow(),
    ))


def sync_correspondence_from_workflow(
    req: WorkflowRequest,
    *,
    actor_user_id: int | None,
    note: str | None = None,
) -> object | None:
    """Synchronize source status/current target from a Workflow request.

    The function only stages database changes; the caller owns the transaction.
    """
    kind, item = source_correspondence(req)
    if not item or not actor_user_id:
        return item

    old_status = (getattr(item, "status", None) or ("RECEIVED" if kind == "IN" else "DRAFT")).upper()
    new_status = _mapped_status(getattr(req, "status", None), old_status)
    target = _workflow_target(req)
    old_target = (
        getattr(item, "current_target_kind", None),
        getattr(item, "current_target_id", None),
        getattr(item, "current_target_label", None),
        getattr(item, "current_assignee_id", None),
    )
    new_target = (
        target.get("kind"),
        target.get("id"),
        target.get("label"),
        target.get("user_id"),
    )

    item.status = new_status
    if target.get("kind"):
        item.current_target_kind = target.get("kind")
        item.current_target_id = target.get("id")
        item.current_target_label = target.get("label")
        item.current_assignee_id = target.get("user_id")

    if old_status != new_status or (target.get("kind") and old_target != new_target):
        _append_movement(
            kind=kind,
            item=item,
            actor_user_id=int(actor_user_id),
            action=_movement_action(getattr(req, "status", None)),
            old_status=old_status,
            new_status=new_status,
            target=target,
            note=note or f"مزامنة تلقائية من مسار #{req.id}.",
        )

    # An approved/rejected official outbound reply closes or returns its source
    # inbound record as part of the same transaction.
    if kind == "OUT" and getattr(item, "source_inbound_id", None):
        inbound = db.session.get(InboundMail, int(item.source_inbound_id))
        if inbound and new_status in {"APPROVED", "RETURNED"}:
            inbound_old = (inbound.status or "RECEIVED").upper()
            inbound_new = "COMPLETED" if new_status == "APPROVED" else "RETURNED"
            inbound.status = inbound_new
            if inbound_old != inbound_new:
                _append_movement(
                    kind="IN",
                    item=inbound,
                    actor_user_id=int(actor_user_id),
                    action="FINAL_REPLY" if inbound_new == "COMPLETED" else "WORKFLOW_REJECTED",
                    old_status=inbound_old,
                    new_status=inbound_new,
                    target={"kind": "OUTBOUND", "id": item.id, "label": f"صادر رقم {item.ref_no or item.id}", "user_id": None},
                    note=(
                        f"اكتمل الرد الرسمي عبر الصادر رقم {item.ref_no or item.id} ومسار #{req.id}."
                        if inbound_new == "COMPLETED"
                        else f"أُعيد الرد الرسمي المرتبط بالصادر رقم {item.ref_no or item.id} من مسار #{req.id}."
                    ),
                )

    return item
