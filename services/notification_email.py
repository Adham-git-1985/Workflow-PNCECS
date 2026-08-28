"""Email delivery for portal and workflow notifications."""

from __future__ import annotations

from datetime import datetime, timedelta
from html import escape

from flask import current_app
from sqlalchemy import or_

from extensions import db
from models import Notification, NotificationEmailDelivery, User
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
