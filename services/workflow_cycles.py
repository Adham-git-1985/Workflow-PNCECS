"""Helpers for grouping correspondence workflows into visible cycles.

One correspondence thread can be acted on more than once.  A cycle is a
normal ``WorkflowRequest`` with a stable parent/root id; this keeps the
existing workflow engine and audit trail intact while allowing the UI to show
one path with several coloured tabs.
"""

from __future__ import annotations

from sqlalchemy import or_, func

from extensions import db
from models import WorkflowRequest


# The palette is intentionally colour-blind-friendly and is used consistently
# in the cycle tabs, badges, and story timeline.  Text labels and the cycle
# number are always rendered as well; colour is never the only identifier.
CYCLE_PALETTE = (
    {
        "name": "أزرق",
        "value": "#2563eb",
        "soft": "#eff6ff",
        "border": "#93c5fd",
    },
    {
        "name": "بنفسجي",
        "value": "#7c3aed",
        "soft": "#f5f3ff",
        "border": "#c4b5fd",
    },
    {
        "name": "تركوازي",
        "value": "#0f766e",
        "soft": "#f0fdfa",
        "border": "#99f6e4",
    },
    {
        "name": "برتقالي",
        "value": "#c2410c",
        "soft": "#fff7ed",
        "border": "#fdba74",
    },
    {
        "name": "وردي",
        "value": "#be185d",
        "soft": "#fdf2f8",
        "border": "#f9a8d4",
    },
    {
        "name": "أخضر",
        "value": "#15803d",
        "soft": "#f0fdf4",
        "border": "#86efac",
    },
)


def cycle_style(cycle_number: int | None) -> dict:
    """Return the stable display style for one cycle number."""
    try:
        number = max(1, int(cycle_number or 1))
    except (TypeError, ValueError):
        number = 1
    return dict(CYCLE_PALETTE[(number - 1) % len(CYCLE_PALETTE)])


def cycle_color(cycle_number: int | None) -> str:
    return cycle_style(cycle_number)["value"]


def cycle_root_id(request_obj: WorkflowRequest | None) -> int | None:
    """Return the root request id for a request or cycle."""
    if not request_obj:
        return None
    try:
        return int(request_obj.parent_request_id or request_obj.id)
    except (TypeError, ValueError):
        return None


def cycle_root(request_obj: WorkflowRequest | None) -> WorkflowRequest | None:
    root_id = cycle_root_id(request_obj)
    if not root_id:
        return None
    if request_obj and int(getattr(request_obj, "id", 0) or 0) == root_id:
        return request_obj
    return db.session.get(WorkflowRequest, root_id)


def cycle_query(root_id: int):
    """Return all cycles in one path group, oldest first."""
    root_id = int(root_id)
    return (
        WorkflowRequest.query
        .filter(or_(
            WorkflowRequest.id == root_id,
            WorkflowRequest.parent_request_id == root_id,
        ))
        .order_by(
            WorkflowRequest.cycle_number.asc(),
            WorkflowRequest.created_at.asc(),
            WorkflowRequest.id.asc(),
        )
    )


def cycles_for(request_obj: WorkflowRequest | None) -> list[WorkflowRequest]:
    root_id = cycle_root_id(request_obj)
    if not root_id:
        return []
    rows = cycle_query(root_id).all()
    # Legacy rows may predate cycle_number.  Keep the visual order stable and
    # allow the caller to persist the style later without changing history.
    for index, row in enumerate(rows, start=1):
        if not getattr(row, "cycle_number", None):
            row.cycle_number = index
    return rows


def next_cycle_number(root_id: int) -> int:
    """Return the next cycle number in a path group."""
    root_id = int(root_id)
    group_filter = or_(
        WorkflowRequest.id == root_id,
        WorkflowRequest.parent_request_id == root_id,
    )
    maximum = db.session.query(func.max(WorkflowRequest.cycle_number)).filter(group_filter).scalar()
    group_count = db.session.query(WorkflowRequest.id).filter(group_filter).count()
    try:
        # Legacy rows can have NULL cycle numbers in databases upgraded before
        # the cycle feature.  Their first linked reply is still cycle 2, not a
        # second cycle 1; the count also protects against mixed legacy/new rows.
        return max(1, int(maximum or 0) + 1, int(group_count or 0) + 1)
    except (TypeError, ValueError):
        return max(1, int(group_count or 0) + 1)


def attach_cycle_display(row: WorkflowRequest, *, is_current: bool = False) -> dict:
    """Build the small serializable context used by cycle tabs."""
    number = int(getattr(row, "cycle_number", None) or 1)
    style = cycle_style(number)
    status = (getattr(row, "status", None) or "").strip().upper()
    status_labels = {
        "DRAFT": "مسودة",
        "IN_PROGRESS": "مفتوحة",
        "APPROVED": "مكتملة",
        "REJECTED": "موقوفة",
        "CLOSED": "مغلقة",
    }
    return {
        "id": int(row.id),
        "number": number,
        "status": status,
        "status_label": status_labels.get(status, status or "غير محددة"),
        "color": getattr(row, "cycle_color", None) or style["value"],
        "soft_color": style["soft"],
        "border_color": style["border"],
        "label": f"الدورة {number}",
        "title": (getattr(row, "title", None) or "").strip(),
        "created_at": getattr(row, "cycle_opened_at", None) or getattr(row, "created_at", None),
        "is_current": bool(is_current),
    }


def cycle_participant_ids(root_id: int) -> set[int]:
    """Collect users who took part in any workflow cycle.

    Imports are intentionally local: this service is imported by the workflow
    routes before the workflow engine is fully initialised.
    """
    from models import (
        AuditLog,
        WorkflowInstance,
        WorkflowInstanceStep,
        WorkflowRequestView,
        WorkflowStepTask,
    )
    from workflow.engine import resolve_step_approver_user_ids

    try:
        root = db.session.get(WorkflowRequest, int(root_id))
        rows = cycles_for(root)
    except (TypeError, ValueError):
        return set()
    if not rows:
        return set()

    request_ids = [int(row.id) for row in rows if getattr(row, "id", None)]
    participant_ids = {
        int(row.requester_id)
        for row in rows
        if getattr(row, "requester_id", None)
    }

    instances = WorkflowInstance.query.filter(
        WorkflowInstance.request_id.in_(request_ids)
    ).all()
    instance_ids = [int(row.id) for row in instances if getattr(row, "id", None)]
    participant_ids.update(
        int(row.last_step_actor_id)
        for row in instances
        if getattr(row, "last_step_actor_id", None)
    )

    if instance_ids:
        steps = WorkflowInstanceStep.query.filter(
            WorkflowInstanceStep.instance_id.in_(instance_ids)
        ).all()
        for step in steps:
            for user_id in (
                getattr(step, "approver_user_id", None),
                getattr(step, "decided_by_id", None),
            ):
                if user_id:
                    participant_ids.add(int(user_id))
            try:
                participant_ids.update(resolve_step_approver_user_ids(step))
            except Exception:
                pass

        participant_ids.update(
            int(task.assignee_user_id)
            for task in WorkflowStepTask.query.filter(
                WorkflowStepTask.instance_id.in_(instance_ids)
            ).all()
            if getattr(task, "assignee_user_id", None)
        )

    participant_ids.update(
        int(user_id)
        for (user_id,) in db.session.query(WorkflowRequestView.user_id)
        .filter(WorkflowRequestView.request_id.in_(request_ids))
        .all()
        if user_id
    )

    # A cycle follower grant is request-scoped, so preserve access and
    # notification delivery for people who were explicitly added before.
    participant_ids.update(
        int(log.target_id)
        for log in AuditLog.query.filter(
            AuditLog.request_id.in_(request_ids),
            AuditLog.action == "WORKFLOW_CYCLE_FOLLOWER",
            AuditLog.target_type == "USER",
            AuditLog.target_id.isnot(None),
        ).all()
        if log.target_id
    )
    return participant_ids
