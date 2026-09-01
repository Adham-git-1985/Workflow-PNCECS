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
from services.hr_request_workflow import KIND_LEAVE, KIND_PERMISSION, can_view_request
from services.workflow_task_email import (
    FAILED,
    MAX_ATTEMPTS,
    PENDING,
    SENT,
    _mail_config,
    _portal_url,
    _send_email,
    _valid_email,
)


_TROUBLE_TICKET_LINK_RE = re.compile(r"^/portal/trouble-tickets/(\d+)(?:[/?#]|$)")
_HR_REQUEST_LINK_RE = re.compile(r"^/portal/hr/approvals/(leaves|permissions)/(\d+)(?:[/?#]|$)")
_TROUBLE_TICKET_ADMIN_ROLE_CODES = {"ADMIN", "SUPER_ADMIN", "SUPERADMIN"}
_TROUBLE_TICKET_NOTIFICATION_TYPE = "TROUBLE_TICKET"
_TROUBLE_TICKET_REQUESTER_NOTIFICATION_TYPE = "TROUBLE_TICKET_REQUESTER_UPDATE"


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
    """Keep ticket emails to Admin/SuperAdmin, except direct creator updates.

    The link check also protects old queued ticket notifications created before
    the role restriction was introduced.
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
    return _user_has_ticket_admin_role(user)


def _can_receive_hr_request_notification_email(user: User, notification: Notification) -> bool:
    """Reject queued HR-request emails for users who cannot open the request."""
    link_match = _HR_REQUEST_LINK_RE.match((getattr(notification, "link_url", None) or "").strip())
    if not link_match:
        return True

    kind = KIND_LEAVE if link_match.group(1) == "leaves" else KIND_PERMISSION
    request_id = int(link_match.group(2))
    row = db.session.get(HRLeaveRequest if kind == KIND_LEAVE else HRPermissionRequest, request_id)
    if not row:
        return False
    if int(user.id) == int(row.user_id):
        return True
    return can_view_request(user, kind, request_id)


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


def send_pending_notification_emails(limit: int = 100, now: datetime | None = None) -> int:
    """Send due general notification emails from the durable outbox."""
    config = _mail_config()
    if not config["ready"]:
        return 0

    now = now or datetime.utcnow()
    exhausted_deliveries = (
        NotificationEmailDelivery.query
        .filter(
            NotificationEmailDelivery.status == PENDING,
            NotificationEmailDelivery.attempt_count >= MAX_ATTEMPTS,
        )
        .all()
    )
    for delivery in exhausted_deliveries:
        delivery.status = FAILED
        delivery.next_attempt_at = None
        delivery.last_error = delivery.last_error or "Maximum email delivery attempts reached."
    if exhausted_deliveries:
        db.session.commit()

    deliveries = (
        NotificationEmailDelivery.query
        .filter(
            NotificationEmailDelivery.status == PENDING,
            NotificationEmailDelivery.attempt_count < MAX_ATTEMPTS,
            or_(
                NotificationEmailDelivery.next_attempt_at.is_(None),
                NotificationEmailDelivery.next_attempt_at <= now,
            ),
        )
        .order_by(NotificationEmailDelivery.created_at.asc(), NotificationEmailDelivery.id.asc())
        .limit(max(1, min(int(limit), 200)))
        .all()
    )

    sent = 0
    for delivery in deliveries:
        notification = db.session.get(Notification, delivery.notification_id)
        user = db.session.get(User, delivery.user_id)
        recipient = _valid_email(getattr(user, "email", None))
        if not notification or not user or not recipient:
            delivery.status = FAILED
            delivery.last_error = "Notification or recipient email address is unavailable."
            db.session.commit()
            continue
        if not (
            _can_receive_ticket_notification_email(user, notification)
            and _can_receive_hr_request_notification_email(user, notification)
        ):
            delivery.status = FAILED
            delivery.last_error = "Recipient is not authorized for this notification."
            delivery.next_attempt_at = None
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
                delay_minutes = min(60, 2 ** delivery.attempt_count)
                delivery.next_attempt_at = now + timedelta(minutes=delay_minutes)
            db.session.commit()
            current_app.logger.warning(
                "Notification email delivery failed id=%s attempt=%s",
                delivery.id,
                delivery.attempt_count,
            )
            continue

        delivery.status = SENT
        delivery.sent_at = now
        delivery.last_error = None
        delivery.next_attempt_at = None
        db.session.commit()
        sent += 1
    return sent
