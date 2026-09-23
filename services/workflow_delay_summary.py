"""Daily summary notifications for overdue workflow paths.

The workflow engine freezes the SLA deadline on the active runtime step in
``WorkflowInstanceStep.due_at``.  This module keeps the daily summary and the
privileged read-only page on that same source of truth.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import and_

from extensions import db
from models import (
    Notification,
    User,
    WorkflowInstance,
    WorkflowInstanceStep,
    WorkflowRequest,
    WorkflowTemplate,
    _global_notification_observer_user_ids,
)
from utils.timezone import to_local_time


WORKFLOW_DELAY_SUMMARY_PATH = "/workflow/delayed"
WORKFLOW_DELAY_SUMMARY_EVENT_PREFIX = "WORKFLOW_DELAY_SUMMARY"
WORKFLOW_DELAY_SUMMARY_HOUR = 8
WORKFLOW_DELAY_SUMMARY_MINUTE = 30
WORKFLOW_FINAL_STATUSES = frozenset({"APPROVED", "REJECTED", "CLOSED"})


def _utc_naive(value: datetime | None = None) -> datetime:
    """Return a naive UTC timestamp suitable for database comparisons."""
    value = value or datetime.utcnow()
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _is_sla_suspended(step: WorkflowInstanceStep | None) -> bool:
    try:
        return int(getattr(step, "sla_days", None)) == -1
    except (TypeError, ValueError):
        return False


def _overdue_row(
    request_row: WorkflowRequest,
    instance: WorkflowInstance,
    step: WorkflowInstanceStep,
    template: WorkflowTemplate | None,
    now: datetime,
) -> dict | None:
    """Build a display row when the active step has crossed its SLA."""
    if _is_sla_suspended(step):
        return None

    due_at = getattr(step, "due_at", None)
    if due_at:
        if due_at >= now:
            return None
    else:
        # Runtime deadlines are authoritative.  The template fallback keeps
        # older installations useful when a runtime step was created before
        # ``due_at`` was introduced.
        try:
            template_days = int(getattr(template, "sla_days_default", 0) or 0)
        except (TypeError, ValueError):
            template_days = 0
        created_at = getattr(request_row, "created_at", None)
        if not template_days or not created_at:
            return None
        due_at = created_at + timedelta(days=template_days)
        if due_at >= now:
            return None

    overdue_seconds = max(0, int((now - due_at).total_seconds()))
    return {
        "request": request_row,
        "instance": instance,
        "step": step,
        "template": template,
        "due_at": due_at,
        "overdue_days": max(1, (overdue_seconds + 86399) // 86400),
    }


def get_overdue_workflow_rows(*, now: datetime | None = None) -> list[dict]:
    """Return open workflow paths whose active step is overdue.

    Only the current pending step is considered.  A completed workflow or a
    later historical step must not keep a path in the daily alert list.
    """
    current_time = _utc_naive(now)
    rows = (
        db.session.query(
            WorkflowRequest,
            WorkflowInstance,
            WorkflowInstanceStep,
            WorkflowTemplate,
        )
        .join(
            WorkflowInstance,
            WorkflowInstance.request_id == WorkflowRequest.id,
        )
        .join(
            WorkflowInstanceStep,
            and_(
                WorkflowInstanceStep.instance_id == WorkflowInstance.id,
                WorkflowInstanceStep.step_order == WorkflowInstance.current_step_order,
            ),
        )
        .outerjoin(
            WorkflowTemplate,
            WorkflowTemplate.id == WorkflowInstance.template_id,
        )
        .filter(
            WorkflowInstance.is_completed.is_(False),
            WorkflowInstanceStep.status == "PENDING",
            WorkflowRequest.status.notin_(WORKFLOW_FINAL_STATUSES),
        )
        .order_by(
            WorkflowInstanceStep.due_at.asc(),
            WorkflowRequest.created_at.asc(),
            WorkflowRequest.id.asc(),
        )
        .all()
    )

    overdue_rows = []
    for request_row, instance, step, template in rows:
        row = _overdue_row(request_row, instance, step, template, current_time)
        if row is not None:
            overdue_rows.append(row)
    return overdue_rows


def workflow_delay_summary_recipient_ids() -> set[int]:
    """Resolve the Secretary General, Super Admins and global observers."""
    recipient_ids: set[int] = set()

    # Keep the global-observer audience identical to the existing notification
    # fan-out mechanism.  It deliberately uses explicit user/role grants and
    # does not rely on the broad ADMIN permission inheritance.
    try:
        recipient_ids.update(_global_notification_observer_user_ids(db.session))
    except Exception:
        pass

    try:
        from permissions import _user_is_super_admin

        for user in User.query.all():
            try:
                if _user_is_super_admin(user):
                    recipient_ids.add(int(user.id))
            except Exception:
                continue
    except Exception:
        pass

    # The existing resolver also recognises legacy role/title and hierarchy
    # assignments for the Secretary General.
    secretary_resolved = False
    try:
        from services.hr_request_workflow import secretary_general_user_ids

        recipient_ids.update(int(user_id) for user_id in secretary_general_user_ids())
        secretary_resolved = True
    except Exception:
        # A partially migrated deployment still gets the canonical role
        # fallback below.
        pass

    if not secretary_resolved:
        try:
            from utils.role_codes import canonical_role_key

            for user in User.query.all():
                role_key = canonical_role_key(getattr(user, "role", None))
                title_key = canonical_role_key(getattr(user, "job_title", None))
                if {role_key, title_key}.intersection({
                    "GENERALSECRETARY",
                    "SECRETARYGENERAL",
                }):
                    recipient_ids.add(int(user.id))
        except Exception:
            pass

    if not recipient_ids:
        return set()

    return {
        int(user_id)
        for (user_id,) in db.session.query(User.id)
        .filter(User.id.in_(recipient_ids))
        .all()
        if user_id
    }


def is_workflow_delay_summary_viewer(user: User | None) -> bool:
    """Whether ``user`` may view the daily overdue-path report."""
    if not user or not getattr(user, "id", None):
        return False
    try:
        return int(user.id) in workflow_delay_summary_recipient_ids()
    except Exception:
        return False


def is_workflow_request_overdue(request_id: int, *, now: datetime | None = None) -> bool:
    """Check whether one request is still represented in the overdue report."""
    try:
        request_id = int(request_id)
    except (TypeError, ValueError):
        return False
    return any(
        int(row["request"].id) == request_id
        for row in get_overdue_workflow_rows(now=now)
    )


def workflow_delay_summary_event_key(local_day: date) -> str:
    return f"{WORKFLOW_DELAY_SUMMARY_EVENT_PREFIX}:{local_day.isoformat()}"


def workflow_delay_summary_message(delayed_count: int) -> str:
    if delayed_count:
        return (
            f"يوجد حاليًا {delayed_count} مسارًا عليها تنبيهات تأخير تلقائية. "
            "اضغط هنا لعرض المسارات والطلبات المتأخرة."
        )
    return (
        "لا توجد حاليًا مسارات عليها تنبيهات تأخير تلقائية. "
        "اضغط هنا لعرض صفحة المسارات المتأخرة."
    )


def send_daily_workflow_delay_summary(
    *,
    now: datetime | None = None,
    force: bool = False,
) -> dict:
    """Create one in-app summary per recipient for the current local day.

    ``force`` is intended for tests or an administrator-triggered diagnostic;
    normal background execution only runs at/after 08:30 local time.
    """
    utc_now = _utc_naive(now)
    local_now = to_local_time(utc_now)
    scheduled_at = time(WORKFLOW_DELAY_SUMMARY_HOUR, WORKFLOW_DELAY_SUMMARY_MINUTE)
    if not force and local_now.timetz().replace(tzinfo=None) < scheduled_at:
        return {
            "sent": 0,
            "delayed_count": 0,
            "recipient_count": 0,
            "status": "before_schedule",
        }

    delayed_rows = get_overdue_workflow_rows(now=utc_now)
    delayed_count = len(delayed_rows)
    recipient_ids = workflow_delay_summary_recipient_ids()
    local_day = local_now.date()
    event_key = workflow_delay_summary_event_key(local_day)

    if not recipient_ids:
        return {
            "sent": 0,
            "delayed_count": delayed_count,
            "recipient_count": 0,
            "event_key": event_key,
            "status": "no_recipients",
        }

    existing_ids = {
        int(user_id)
        for (user_id,) in (
            db.session.query(Notification.user_id)
            .filter(
                Notification.user_id.in_(recipient_ids),
                Notification.event_key == event_key,
                Notification.source == "workflow",
                Notification.is_mirror.is_(False),
            )
            .all()
        )
        if user_id
    }
    missing_ids = sorted(recipient_ids.difference(existing_ids))
    if missing_ids:
        message = workflow_delay_summary_message(delayed_count)
        notification_type = "WARNING" if delayed_count else "INFO"
        db.session.add_all(
            Notification(
                user_id=user_id,
                message=message,
                type=notification_type,
                role="WORKFLOW_DELAY_SUMMARY",
                source="workflow",
                link_url=WORKFLOW_DELAY_SUMMARY_PATH,
                event_key=event_key,
                actor_id=None,
                is_mirror=False,
                is_read=False,
                is_visible=True,
                email_delivery_mode="GENERAL",
                created_at=utc_now,
            )
            for user_id in missing_ids
        )
        db.session.commit()

    return {
        "sent": len(missing_ids),
        "delayed_count": delayed_count,
        "recipient_count": len(recipient_ids),
        "event_key": event_key,
        "status": "sent" if missing_ids else "already_sent",
    }
