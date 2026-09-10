"""Email delivery for portal and workflow notifications."""

from __future__ import annotations

from datetime import datetime, timedelta
from html import escape
import re
import unicodedata

from flask import current_app
from sqlalchemy import func, or_

from extensions import db
from models import HRLeaveRequest, HRPermissionRequest, Notification, NotificationEmailDelivery, Role, TroubleTicket, User
from services.delivery_controls import (
    EMAIL_DISABLED_REASON,
    email_delivery_enabled,
)
from services.hr_request_workflow import (
    KIND_LEAVE,
    KIND_PERMISSION,
    can_receive_request_notification,
)
from services.workflow_task_email import (
    FAILED,
    MAX_ATTEMPTS,
    PENDING,
    SENT,
    _mail_config,
    _portal_url,
    _send_email,
    resolve_user_delivery_email,
)


_TROUBLE_TICKET_LINK_RE = re.compile(r"^/portal/trouble-tickets/(\d+)(?:[/?#]|$)")
_HR_REQUEST_LINK_RE = re.compile(r"^/portal/hr/approvals/(leaves|permissions)/(\d+)(?:[/?#]|$)")
_TROUBLE_TICKET_ADMIN_ROLE_CODES = {"ADMIN", "SUPER_ADMIN", "SUPERADMIN"}
_TROUBLE_TICKET_NOTIFICATION_TYPE = "TROUBLE_TICKET"
_TROUBLE_TICKET_REQUESTER_NOTIFICATION_TYPE = "TROUBLE_TICKET_REQUESTER_UPDATE"
ATTENDANCE_SCHEDULE_EMAIL_MODE = "ATTENDANCE_SCHEDULE"
NOTIFICATION_EMAILS_DISABLED_REASON = "Notification emails are disabled; the notification remains available in the system."
EMAIL_UNAVAILABLE_CANCELLED_REASON = "Skipped: recipient has no configured delivery email address."


def _normalize_trouble_ticket_role(value: str | None) -> str:
    normalized = (value or "").strip().upper().replace("-", "_").replace(" ", "_")
    try:
        normalized = unicodedata.normalize("NFKC", normalized)
        return "".join(ch for ch in normalized if ch.isalnum() or ch == "_")
    except Exception:
        return normalized


def _user_has_ticket_admin_role(user: User) -> bool:
    """Match the strict ticket viewer roles without delegated permissions."""
    raw_role = (getattr(user, "role", None) or "").strip()
    role_code = _normalize_trouble_ticket_role(raw_role)
    if role_code in _TROUBLE_TICKET_ADMIN_ROLE_CODES:
        return True
    if not raw_role:
        return False
    try:
        role_row = Role.query.filter(
            or_(
                func.upper(Role.code) == role_code,
                Role.name_ar == raw_role,
                func.lower(Role.name_en) == raw_role.lower(),
            )
        ).first()
        return bool(
            role_row
            and _normalize_trouble_ticket_role(role_row.code) in _TROUBLE_TICKET_ADMIN_ROLE_CODES
        )
    except Exception:
        return False


def _can_receive_ticket_notification_email(user: User, notification: Notification) -> bool:
    """Allow ticket emails only to admins, the creator, or current assignee.

    The link check also protects old queued ticket notifications created before
    assignment was supported for any user.
    """
    notification_type = (getattr(notification, "type", None) or "").strip().upper()
    link_match = _TROUBLE_TICKET_LINK_RE.match((getattr(notification, "link_url", None) or "").strip())
    ticket_notification_types = {
        _TROUBLE_TICKET_NOTIFICATION_TYPE,
        _TROUBLE_TICKET_REQUESTER_NOTIFICATION_TYPE,
    }
    if notification_type not in ticket_notification_types and not link_match:
        return True

    ticket_id = int(link_match.group(1)) if link_match else None
    if not ticket_id:
        return False
    ticket = db.session.get(TroubleTicket, ticket_id)
    if not ticket:
        return False
    if notification_type == _TROUBLE_TICKET_REQUESTER_NOTIFICATION_TYPE:
        return ticket.requester_id is not None and int(user.id) == int(ticket.requester_id)
    return bool(
        _user_has_ticket_admin_role(user)
        or (
            ticket.assigned_to_id is not None
            and int(user.id) == int(ticket.assigned_to_id)
        )
    )


def _can_receive_hr_request_notification_email(user: User, notification: Notification) -> bool:
    """Limit HR-request email to the requester and current assigned approvers."""
    link_match = _HR_REQUEST_LINK_RE.match((getattr(notification, "link_url", None) or "").strip())
    if not link_match:
        return True

    kind = KIND_LEAVE if link_match.group(1) == "leaves" else KIND_PERMISSION
    request_id = int(link_match.group(2))
    row = db.session.get(HRLeaveRequest if kind == KIND_LEAVE else HRPermissionRequest, request_id)
    if not row:
        return False
    return can_receive_request_notification(user, kind, request_id)


def _email_content(user: User, notification: Notification) -> tuple[str, str, str]:
    recipient_name = (user.full_name or user.name or user.email or "المستخدم").strip()
    message = (notification.message or "لديك تحديث جديد في نظام مسار.").strip()
    notification_url = _portal_url(notification.link_url)
    subject = f"تحديث جديد في نظام مسار — {message}"[:200]
    action_text = "فتح التحديث في النظام"

    text_body = "\n".join((
        f"السلام عليكم {recipient_name}،",
        "",
        message,
        "",
        f"{action_text}: {notification_url}",
        "",
        "هذه رسالة آلية من نظام مسار.",
    ))
    html_body = f"""\
    <html><body dir="rtl" style="font-family:Arial,sans-serif;color:#1f2937;line-height:1.8">
      <h2 style="color:#0f766e">تحديث جديد في نظام مسار</h2>
      <p>السلام عليكم {escape(recipient_name)}،</p>
      <p>{escape(message)}</p>
      <p><a href="{escape(notification_url, quote=True)}" style="display:inline-block;padding:10px 18px;background:#0f766e;color:#ffffff;text-decoration:none;border-radius:5px">{action_text}</a></p>
      <p style="color:#6b7280;font-size:12px">هذه رسالة آلية من نظام مسار.</p>
    </body></html>
    """
    return subject, text_body, html_body


def enqueue_notification_email(notification: Notification) -> bool:
    """Queue email only for explicitly opted-in attendance notifications."""
    if not notification:
        return False
    if (notification.email_delivery_mode or "").strip().upper() != ATTENDANCE_SCHEDULE_EMAIL_MODE:
        return False
    if not email_delivery_enabled():
        return False

    user = db.session.get(User, notification.user_id)
    if not resolve_user_delivery_email(user):
        return False
    if notification.id is None:
        db.session.flush()
    if NotificationEmailDelivery.query.filter_by(notification_id=notification.id).first():
        return False

    db.session.add(NotificationEmailDelivery(
        notification_id=notification.id,
        user_id=notification.user_id,
        status=PENDING,
        attempt_count=0,
    ))
    return True


def send_pending_notification_emails(limit: int = 100, now: datetime | None = None) -> int:
    """Send opted-in attendance mail and cancel legacy notification mail."""
    pending_deliveries = NotificationEmailDelivery.query.filter_by(status=PENDING).all()
    attendance_deliveries = []
    legacy_deliveries = []
    for delivery in pending_deliveries:
        mode = (
            getattr(delivery.notification, "email_delivery_mode", "") or ""
        ).strip().upper()
        if mode == ATTENDANCE_SCHEDULE_EMAIL_MODE:
            attendance_deliveries.append(delivery)
        else:
            legacy_deliveries.append(delivery)

    for delivery in legacy_deliveries:
        delivery.status = "CANCELLED"
        delivery.next_attempt_at = None
        delivery.last_error = NOTIFICATION_EMAILS_DISABLED_REASON
    if legacy_deliveries:
        db.session.commit()

    if not attendance_deliveries:
        return 0
    if not email_delivery_enabled():
        for delivery in attendance_deliveries:
            delivery.status = "CANCELLED"
            delivery.next_attempt_at = None
            delivery.last_error = EMAIL_DISABLED_REASON
        db.session.commit()
        return 0

    config = _mail_config()
    if not config["ready"]:
        return 0

    now = now or datetime.utcnow()
    for delivery in attendance_deliveries:
        if int(delivery.attempt_count or 0) >= MAX_ATTEMPTS:
            delivery.status = FAILED
            delivery.next_attempt_at = None
            delivery.last_error = delivery.last_error or "Maximum email delivery attempts reached."
    db.session.commit()

    due_deliveries = [
        delivery
        for delivery in attendance_deliveries
        if delivery.status == PENDING
        and int(delivery.attempt_count or 0) < MAX_ATTEMPTS
        and (delivery.next_attempt_at is None or delivery.next_attempt_at <= now)
    ]
    due_deliveries.sort(key=lambda row: (row.created_at or datetime.min, row.id or 0))
    due_deliveries = due_deliveries[:max(1, min(int(limit), 200))]

    sent = 0
    for delivery in due_deliveries:
        notification = db.session.get(Notification, delivery.notification_id)
        user = db.session.get(User, delivery.user_id)
        if not notification or not user:
            delivery.status = FAILED
            delivery.next_attempt_at = None
            delivery.last_error = "Recipient or notification is unavailable."
            db.session.commit()
            continue

        recipient = resolve_user_delivery_email(user)
        if not recipient:
            delivery.status = "CANCELLED"
            delivery.next_attempt_at = None
            delivery.last_error = EMAIL_UNAVAILABLE_CANCELLED_REASON
            db.session.commit()
            continue

        try:
            subject, text_body, html_body = _email_content(user, notification)
            _send_email(config, recipient, subject, text_body, html_body)
        except Exception as exc:
            delivery.attempt_count += 1
            delivery.last_error = str(exc)[:500]
            if delivery.attempt_count >= MAX_ATTEMPTS:
                delivery.status = FAILED
                delivery.next_attempt_at = None
            else:
                delivery.next_attempt_at = now + timedelta(
                    minutes=min(60, 2 ** delivery.attempt_count)
                )
            db.session.commit()
            current_app.logger.warning(
                "Attendance schedule email delivery failed id=%s attempt=%s",
                delivery.id,
                delivery.attempt_count,
            )
            continue

        delivery.status = SENT
        delivery.sent_at = now
        delivery.next_attempt_at = None
        delivery.last_error = None
        db.session.commit()
        sent += 1
    return sent
