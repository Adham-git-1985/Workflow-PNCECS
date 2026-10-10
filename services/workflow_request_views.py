"""Per-user read state for procedural workflow requests."""

from __future__ import annotations

from datetime import datetime
from typing import Mapping

from extensions import db
from models import WorkflowInstance, WorkflowRequestView


WORKFLOW_NEW_BADGE_EXCLUDED_STATUSES = frozenset({
    "DRAFT",
    "APPROVED",
    "REJECTED",
    "CLOSED",
})


def workflow_request_can_be_new(status: str | None) -> bool:
    """Return whether a request status represents live workflow work."""
    return (status or "").strip().upper() not in WORKFLOW_NEW_BADGE_EXCLUDED_STATUSES


def workflow_current_step_orders(request_ids) -> dict[int, int]:
    """Load current step numbers in bounded batches; missing instances use zero."""
    normalized_ids = list(dict.fromkeys(
        int(request_id) for request_id in request_ids if request_id
    ))
    current_steps: dict[int, int] = {}
    for offset in range(0, len(normalized_ids), 800):
        batch = normalized_ids[offset:offset + 800]
        current_steps.update({
            int(request_id): int(step_order or 0)
            for request_id, step_order in (
                db.session.query(
                    WorkflowInstance.request_id,
                    WorkflowInstance.current_step_order,
                )
                .filter(WorkflowInstance.request_id.in_(batch))
                .all()
            )
        })
    return {
        request_id: current_steps.get(request_id, 0)
        for request_id in normalized_ids
    }


def unopened_workflow_request_ids(
    user_id: int,
    request_step_orders: Mapping[int, int],
) -> set[int]:
    """Return requests whose current step has not been opened by this user."""
    normalized = {
        int(request_id): int(step_order or 0)
        for request_id, step_order in request_step_orders.items()
        if request_id
    }
    if not normalized:
        return set()

    viewed_steps: dict[int, int] = {}
    request_ids = list(normalized)
    # Keep the admin "all requests" page safe on SQLite installations whose
    # bound-parameter limit is lower than the number of stored requests.
    for offset in range(0, len(request_ids), 800):
        batch = request_ids[offset:offset + 800]
        viewed_steps.update({
            int(request_id): int(step_order or 0)
            for request_id, step_order in (
                db.session.query(
                    WorkflowRequestView.request_id,
                    WorkflowRequestView.step_order,
                )
                .filter(
                    WorkflowRequestView.user_id == int(user_id),
                    WorkflowRequestView.request_id.in_(batch),
                )
                .all()
            )
        })
    return {
        request_id
        for request_id, current_step_order in normalized.items()
        if viewed_steps.get(request_id) != current_step_order
    }


def invalidate_workflow_request_views(
    request_id: int,
    *,
    user_ids=None,
) -> int:
    """Make an existing assignment appear new again; caller owns the transaction.

    Reopening a request or reassigning its active step can create a fresh task
    without changing ``current_step_order``. Removing the affected read-state
    rows lets the existing badge logic distinguish that fresh assignment from
    the earlier visit without requiring another schema revision.
    """
    query = WorkflowRequestView.query.filter(
        WorkflowRequestView.request_id == int(request_id),
    )
    if user_ids is not None:
        normalized_user_ids = sorted({
            int(user_id)
            for user_id in user_ids
            if user_id
        })
        if not normalized_user_ids:
            return 0
        query = query.filter(WorkflowRequestView.user_id.in_(normalized_user_ids))
    return int(query.delete(synchronize_session=False) or 0)


def mark_workflow_request_viewed(
    user_id: int,
    request_id: int,
    step_order: int | None,
    *,
    viewed_at: datetime | None = None,
) -> bool:
    """Mark the request's current step as opened; caller owns the transaction."""
    user_id = int(user_id)
    request_id = int(request_id)
    step_order = int(step_order or 0)
    row = WorkflowRequestView.query.filter_by(
        user_id=user_id,
        request_id=request_id,
    ).first()
    timestamp = viewed_at or datetime.utcnow()
    if row is None:
        db.session.add(WorkflowRequestView(
            user_id=user_id,
            request_id=request_id,
            step_order=step_order,
            viewed_at=timestamp,
        ))
        return True
    if int(row.step_order or 0) == step_order:
        return False
    row.step_order = step_order
    row.viewed_at = timestamp
    return True
