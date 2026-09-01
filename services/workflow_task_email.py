"""Email delivery for workflow task assignments and daily reminders."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from email.message import EmailMessage
from email.utils import formataddr
from html import escape
import os
import re
import smtplib
import ssl
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from flask import current_app, has_request_context, request
from sqlalchemy import or_

from extensions import db
from models import (
    SystemSetting,
    User,
    WorkflowInstance,
    WorkflowInstanceStep,
    WorkflowRequest,
    WorkflowStepTask,
    WorkflowTaskEmailDelivery,
)
from services.workflow_confidentiality import filter_confidential_workflow_user_ids
from services.delivery_controls import (
    cancel_pending_email_deliveries,
    email_delivery_enabled,
)
from utils.notification_links import notification_target_path


EMAIL_ENABLED_SETTING = "EMAIL_CIRCULAR_ENABLED"
SMTP_HOST_SETTING = "EMAIL_CIRCULAR_SMTP_HOST"
SMTP_PORT_SETTING = "EMAIL_CIRCULAR_SMTP_PORT"
SMTP_SECURITY_SETTING = "EMAIL_CIRCULAR_SECURITY"
SMTP_USERNAME_SETTING = "EMAIL_CIRCULAR_USERNAME"
SMTP_PASSWORD_SETTING = "EMAIL_CIRCULAR_PASSWORD"
FROM_EMAIL_SETTING = "EMAIL_CIRCULAR_FROM_EMAIL"
FROM_NAME_SETTING = "EMAIL_CIRCULAR_FROM_NAME"
REPLY_TO_SETTING = "EMAIL_CIRCULAR_REPLY_TO"
PUBLIC_URL_SETTING = "EMAIL_CIRCULAR_PUBLIC_URL"
SMTP_PASSWORD_ENV = "PORTAL_EMAIL_PASSWORD"

ASSIGNMENT = "ASSIGNMENT"
DAILY_REMINDER = "DAILY_REMINDER"
PENDING = "PENDING"
SENT = "SENT"
FAILED = "FAILED"
MAX_ATTEMPTS = 2
DAILY_REMINDER_TIME = time(hour=8, minute=30)
DAILY_REMINDER_TIMEZONE = "Asia/Jerusalem"


def _setting(key: str, default: str = "") -> str:
    row = SystemSetting.query.filter_by(key=key).first()
    return str(row.value) if row and row.value not in (None, "") else default


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _valid_email(value: str | None) -> str:
    email = str(value or "").strip()
    if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return email
    return ""


def _valid_public_url(value: str | None) -> str:
    url = str(value or "").strip().rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return url
    return ""


def _mail_config() -> dict:
    enabled = _enabled(_setting(EMAIL_ENABLED_SETTING, "0"))
    host = _setting(SMTP_HOST_SETTING).strip()
    try:
        port = max(1, min(int(_setting(SMTP_PORT_SETTING, "587")), 65535))
    except (TypeError, ValueError):
        port = 587
    security = _setting(SMTP_SECURITY_SETTING, "starttls").strip().lower()
    if security not in {"starttls", "ssl", "none"}:
        security = "starttls"

    username = _setting(SMTP_USERNAME_SETTING).strip()
    password = (_setting(SMTP_PASSWORD_SETTING) or os.getenv(SMTP_PASSWORD_ENV, "")).strip()
    from_email = _valid_email(_setting(FROM_EMAIL_SETTING) or username)
    from_name = _setting(FROM_NAME_SETTING, "البوابة الإدارية").strip() or "البوابة الإدارية"
    reply_to = _valid_email(_setting(REPLY_TO_SETTING))

    return {
        "enabled": enabled,
        "ready": enabled and bool(host and from_email and (not username or password)),
        "host": host,
        "port": port,
        "security": security,
        "username": username,
        "password": password,
        "from_email": from_email,
        "from_name": from_name,
        "reply_to": reply_to,
    }


def _portal_url(link_url: str | None = None) -> str:
    """Return an absolute portal URL using the configured public address."""
    path = str(link_url or "/").strip() or "/"
    parsed = urlsplit(path)
    absolute_url = path if parsed.scheme in {"http", "https"} and parsed.netloc else ""
    if absolute_url:
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

    # A configured public URL takes priority over the address used by the
    # browser that happened to create the task (for example, 127.0.0.1).
    base_url = _valid_public_url(_setting(PUBLIC_URL_SETTING))
    if not base_url and has_request_context():
        base_url = _valid_public_url(request.url_root)
    if not base_url:
        base_url = _valid_public_url(current_app.config.get("PUBLIC_APP_URL"))
    if not base_url:
        return absolute_url or path
    return urljoin(f"{base_url}/", path.lstrip("/"))


def _task_url(request_id: int, link_url: str | None = None) -> str:
    return _portal_url(link_url or notification_target_path("WorkflowRequest", request_id))


def enqueue_task_assignment_emails(
    workflow_request: WorkflowRequest,
    user_ids,
    *,
    step_order: int,
    instance_id: int | None = None,
    link_url: str | None = None,
) -> int:
    """Add one durable assignment email per newly responsible user."""
    if not email_delivery_enabled():
        return 0
    if not workflow_request or not workflow_request.id or not step_order:
        return 0

    if not instance_id:
        instance = WorkflowInstance.query.filter_by(request_id=workflow_request.id).first()
        instance_id = getattr(instance, "id", None)
    if not instance_id:
        return 0

    task_url = _task_url(workflow_request.id, link_url)
    queued = 0
    for user_id in sorted({int(value) for value in (user_ids or []) if value}):
        existing = WorkflowTaskEmailDelivery.query.filter_by(
            request_id=workflow_request.id,
            instance_id=int(instance_id),
            step_order=int(step_order),
            user_id=user_id,
            delivery_kind=ASSIGNMENT,
            delivery_date="",
        ).first()
        if existing:
            continue
        db.session.add(WorkflowTaskEmailDelivery(
            request_id=workflow_request.id,
            instance_id=int(instance_id),
            step_order=int(step_order),
            user_id=user_id,
            delivery_kind=ASSIGNMENT,
            delivery_date="",
            status=PENDING,
            link_url=task_url,
        ))
        queued += 1
    return queued


def _is_task_still_pending(delivery: WorkflowTaskEmailDelivery) -> bool:
    workflow_instance = db.session.get(WorkflowInstance, delivery.instance_id)
    step = WorkflowInstanceStep.query.filter_by(
        instance_id=delivery.instance_id,
        step_order=delivery.step_order,
    ).first()
    workflow_request = db.session.get(WorkflowRequest, delivery.request_id)
    if not workflow_instance or not step or not workflow_request:
        return False
    if workflow_instance.is_completed or int(workflow_instance.current_step_order or 0) != int(delivery.step_order):
        return False
    if (step.status or "").upper() != "PENDING":
        return False

    allowed_ids = filter_confidential_workflow_user_ids(workflow_request, {int(delivery.user_id)})
    if int(delivery.user_id) not in allowed_ids:
        return False

    explicit_task = WorkflowStepTask.query.filter_by(
        instance_id=delivery.instance_id,
        step_order=delivery.step_order,
        assignee_user_id=delivery.user_id,
        status="PENDING",
    ).first()
    if explicit_task:
        return True

    if (step.mode or "SEQUENTIAL").upper() == "PARALLEL_SYNC":
        return False

    from workflow.engine import resolve_step_approver_user_ids

    return int(delivery.user_id) in resolve_step_approver_user_ids(step)


def _pending_step_task_user_ids(workflow_instance: WorkflowInstance, step: WorkflowInstanceStep) -> set[int]:
    return {
        int(user_id)
        for (user_id,) in db.session.query(WorkflowStepTask.assignee_user_id).filter_by(
            instance_id=workflow_instance.id,
            step_order=step.step_order,
            status="PENDING",
        ).all()
        if user_id
    }


def _email_content(user: User, workflow_request: WorkflowRequest, delivery: WorkflowTaskEmailDelivery) -> tuple[str, str, str]:
    reminder = delivery.delivery_kind == DAILY_REMINDER
    heading = "تذكير يومي: مهمة بانتظار إجراءك" if reminder else "مهمة جديدة بانتظار إجراءك"
    title = (workflow_request.title or "طلب دون عنوان").strip()
    recipient_name = (user.full_name or user.name or user.email or "المستخدم").strip()
    step_label = f"الخطوة الحالية: {delivery.step_order}"
    task_url = _task_url(workflow_request.id, delivery.link_url)
    action_text = "فتح المهمة في النظام"

    subject = f"{heading} — الطلب رقم #{workflow_request.id}"[:200]
    text_body = "\n".join((
        f"السلام عليكم {recipient_name}،",
        "",
        f"{heading} في نظام مسار.",
        f"رقم المهمة: #{workflow_request.id}",
        f"العنوان: {title}",
        step_label,
        "",
        f"{action_text}: {task_url}",
        "",
        "هذه رسالة آلية؛ يرجى الدخول إلى النظام لاتخاذ الإجراء المطلوب.",
    ))
    html_body = f"""\
    <html><body dir="rtl" style="font-family:Arial,sans-serif;color:#1f2937;line-height:1.8">
      <h2 style="color:#0f766e">{escape(heading)}</h2>
      <p>السلام عليكم {escape(recipient_name)}،</p>
      <p>لديك مهمة بانتظار الإجراء في نظام مسار.</p>
      <p><strong>رقم المهمة:</strong> #{workflow_request.id}<br>
      <strong>العنوان:</strong> {escape(title)}<br>
      <strong>{escape(step_label)}</strong></p>
      <p><a href="{escape(task_url, quote=True)}" style="display:inline-block;padding:10px 18px;background:#0f766e;color:#ffffff;text-decoration:none;border-radius:5px">{action_text}</a></p>
      <p style="color:#6b7280;font-size:12px">هذه رسالة آلية؛ يرجى الدخول إلى النظام لاتخاذ الإجراء المطلوب.</p>
    </body></html>
    """
    return subject, text_body, html_body


def _send_email(config: dict, recipient: str, subject: str, text_body: str, html_body: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((config["from_name"], config["from_email"]))
    message["To"] = recipient
    if config["reply_to"]:
        message["Reply-To"] = config["reply_to"]
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    if config["security"] == "ssl":
        smtp = smtplib.SMTP_SSL(
            config["host"],
            config["port"],
            timeout=20,
            context=ssl.create_default_context(),
        )
    else:
        smtp = smtplib.SMTP(config["host"], config["port"], timeout=20)
    try:
        smtp.ehlo()
        if config["security"] == "starttls":
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        if config["username"]:
            smtp.login(config["username"], config["password"])
        smtp.send_message(message, from_addr=config["from_email"], to_addrs=[recipient])
    finally:
        try:
            smtp.quit()
        except Exception:
            pass


def enqueue_daily_task_reminders(today: date | None = None) -> int:
    """Queue one reminder per still-pending task and recipient each day."""
    if not email_delivery_enabled():
        return 0
    if not _mail_config()["enabled"]:
        return 0

    today = today or date.today()
    today_iso = today.isoformat()
    queued = 0
    instances = WorkflowInstance.query.filter_by(is_completed=False).all()
    for workflow_instance in instances:
        step = WorkflowInstanceStep.query.filter_by(
            instance_id=workflow_instance.id,
            step_order=workflow_instance.current_step_order,
            status="PENDING",
        ).first()
        workflow_request = db.session.get(WorkflowRequest, workflow_instance.request_id)
        if not step or not workflow_request:
            continue

        user_ids = _pending_step_task_user_ids(workflow_instance, step)
        if (step.mode or "SEQUENTIAL").upper() != "PARALLEL_SYNC":
            from workflow.engine import resolve_step_approver_user_ids

            user_ids.update(resolve_step_approver_user_ids(step))

        user_ids = set(filter_confidential_workflow_user_ids(workflow_request, user_ids))
        for user_id in user_ids:
            assignment = WorkflowTaskEmailDelivery.query.filter_by(
                request_id=workflow_request.id,
                instance_id=workflow_instance.id,
                step_order=step.step_order,
                user_id=user_id,
                delivery_kind=ASSIGNMENT,
                delivery_date="",
            ).first()
            if assignment and assignment.sent_at and assignment.sent_at.date() == today:
                continue

            exists = WorkflowTaskEmailDelivery.query.filter_by(
                request_id=workflow_request.id,
                instance_id=workflow_instance.id,
                step_order=step.step_order,
                user_id=user_id,
                delivery_kind=DAILY_REMINDER,
                delivery_date=today_iso,
            ).first()
            if exists:
                continue

            db.session.add(WorkflowTaskEmailDelivery(
                request_id=workflow_request.id,
                instance_id=workflow_instance.id,
                step_order=step.step_order,
                user_id=user_id,
                delivery_kind=DAILY_REMINDER,
                delivery_date=today_iso,
                status=PENDING,
                link_url=(assignment.link_url if assignment else _task_url(workflow_request.id)),
            ))
            queued += 1
    return queued


def send_pending_task_emails(limit: int = 50, now: datetime | None = None) -> int:
    """Send due outbox rows. Failed deliveries retry with bounded backoff."""
    if not email_delivery_enabled():
        cancel_pending_email_deliveries()
        db.session.commit()
        return 0
    config = _mail_config()
    if not config["ready"]:
        return 0

    now = now or datetime.utcnow()
    exhausted_deliveries = (
        WorkflowTaskEmailDelivery.query
        .filter(
            WorkflowTaskEmailDelivery.status == PENDING,
            WorkflowTaskEmailDelivery.attempt_count >= MAX_ATTEMPTS,
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
        WorkflowTaskEmailDelivery.query
        .filter(
            WorkflowTaskEmailDelivery.status == PENDING,
            WorkflowTaskEmailDelivery.attempt_count < MAX_ATTEMPTS,
            or_(
                WorkflowTaskEmailDelivery.next_attempt_at.is_(None),
                WorkflowTaskEmailDelivery.next_attempt_at <= now,
            ),
        )
        .order_by(WorkflowTaskEmailDelivery.created_at.asc(), WorkflowTaskEmailDelivery.id.asc())
        .limit(max(1, min(int(limit), 200)))
        .all()
    )

    sent = 0
    for delivery in deliveries:
        if not _is_task_still_pending(delivery):
            delivery.status = "CANCELLED"
            delivery.last_error = "Task is no longer pending for this recipient."
            db.session.commit()
            continue

        user = db.session.get(User, delivery.user_id)
        workflow_request = db.session.get(WorkflowRequest, delivery.request_id)
        recipient = _valid_email(getattr(user, "email", None))
        if not user or not workflow_request or not recipient:
            delivery.status = FAILED
            delivery.last_error = "Recipient email address is unavailable or invalid."
            db.session.commit()
            continue

        try:
            subject, text_body, html_body = _email_content(user, workflow_request, delivery)
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
                "Workflow task email delivery failed id=%s attempt=%s",
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


def _reminder_now(now: datetime | None = None) -> datetime:
    if now is not None:
        return now
    try:
        return datetime.now(ZoneInfo(DAILY_REMINDER_TIMEZONE))
    except Exception:
        return datetime.now()


def run_workflow_task_email_cycle(now: datetime | None = None) -> dict[str, int]:
    """Deliver pending task mail and queue the daily reminder from 08:30 onward."""
    reminder_now = _reminder_now(now)
    queued = 0
    if reminder_now.time() >= DAILY_REMINDER_TIME:
        queued = enqueue_daily_task_reminders(reminder_now.date())
    if queued:
        db.session.commit()
    sent = send_pending_task_emails()
    return {"queued_reminders": queued, "sent": sent}
