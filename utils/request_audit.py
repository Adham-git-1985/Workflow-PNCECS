"""Application-wide request auditing.

Domain modules keep their detailed audit records. This module adds a safety
net so every interactive user request is represented in the central timeline,
including page views and routes that do not yet have a domain-specific audit.
"""

from __future__ import annotations

from datetime import datetime
import time

from flask import g, request
from flask_login import current_user

from extensions import db
from models import AuditLog
from utils.audit_helpers import get_audit_source_summary


AUTOMATED_ENDPOINTS = {
    "static",
    "assistant.chat",
    "workflow.event_stream",
    "workflow.unread_notifications_count",
}

AUTOMATED_PATH_PREFIXES = (
    "/static/",
    "/favicon.ico",
)

SENSITIVE_FIELD_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "csrf",
    "authorization",
    "cookie",
    "session",
    "otp",
    "pin",
)

LONG_TEXT_FIELD_PARTS = (
    "body",
    "content",
    "description",
    "note",
    "comment",
    "message",
    "minutes_text",
    "decisions_text",
)

TARGET_ARGUMENTS = (
    ("request_id", "WorkflowRequest"),
    ("meeting_id", "PORTAL_MEETING"),
    ("file_id", "ARCHIVE_FILE"),
    ("message_id", "Message"),
    ("user_id", "USER"),
    ("task_id", "TASK"),
    ("notif_id", "NOTIFICATION"),
    ("attachment_id", "ATTACHMENT"),
    ("item_id", "ITEM"),
)


def _compact(value, max_len: int = 120) -> str:
    try:
        text = " ".join(str(value or "").split())
    except Exception:
        text = ""
    if len(text) > max_len:
        return text[: max_len - 3].rstrip() + "..."
    return text


def _is_sensitive_field(name: str) -> bool:
    key = (name or "").strip().lower()
    return any(part in key for part in SENSITIVE_FIELD_PARTS)


def _is_long_text_field(name: str) -> bool:
    key = (name or "").strip().lower()
    return any(part in key for part in LONG_TEXT_FIELD_PARTS)


def _summarize_values(source) -> list[str]:
    parts: list[str] = []
    try:
        keys = list(source.keys())[:12]
    except Exception:
        return parts

    for key in keys:
        key_text = _compact(key, 60)
        if not key_text or _is_sensitive_field(key_text):
            continue
        if _is_long_text_field(key_text):
            parts.append(f"{key_text}=<تم إدخال نص>")
            continue
        try:
            values = source.getlist(key) if hasattr(source, "getlist") else [source.get(key)]
        except Exception:
            values = []
        clean_values = [_compact(value, 80) for value in values[:5]]
        clean_values = [value for value in clean_values if value]
        if not clean_values:
            continue
        rendered = ", ".join(clean_values)
        parts.append(f"{key_text}={rendered}")
    return parts


def _request_details() -> str:
    parts: list[str] = []

    query_parts = _summarize_values(request.args)
    if query_parts:
        parts.append("معايير العرض: " + " | ".join(query_parts))

    form_parts = _summarize_values(request.form)
    if form_parts:
        parts.append("البيانات المدخلة: " + " | ".join(form_parts))

    try:
        file_parts = []
        for field, uploaded in list(request.files.items())[:8]:
            if _is_sensitive_field(field):
                continue
            filename = _compact(getattr(uploaded, "filename", ""), 100)
            if filename:
                file_parts.append(f"{_compact(field, 50)}={filename}")
        if file_parts:
            parts.append("الملفات: " + " | ".join(file_parts))
    except Exception:
        pass

    if request.is_json:
        try:
            payload = request.get_json(silent=True)
            if isinstance(payload, dict):
                json_parts = _summarize_values(payload)
                if json_parts:
                    parts.append("بيانات JSON: " + " | ".join(json_parts))
        except Exception:
            pass

    return "\n".join(parts)


def _target_reference() -> tuple[int | None, str | None, int | None]:
    view_args = request.view_args or {}
    request_id = None
    try:
        if view_args.get("request_id") is not None:
            request_id = int(view_args["request_id"])
    except (TypeError, ValueError):
        request_id = None

    for argument, target_type in TARGET_ARGUMENTS:
        raw_value = view_args.get(argument)
        if raw_value is None:
            continue
        try:
            return request_id, target_type, int(raw_value)
        except (TypeError, ValueError):
            continue
    return request_id, None, None


def _should_audit_request() -> bool:
    if request.method in {"HEAD", "OPTIONS"}:
        return False
    if request.endpoint in AUTOMATED_ENDPOINTS:
        return False
    return not any(request.path.startswith(prefix) for prefix in AUTOMATED_PATH_PREFIXES)


def _action_and_summary(status_code: int) -> tuple[str, str]:
    endpoint = request.endpoint or "غير معروف"
    if endpoint == "login" and request.method == "POST":
        return "USER_LOGIN", "سجّل المستخدم الدخول إلى النظام"
    if endpoint == "logout":
        return "USER_LOGOUT", "سجّل المستخدم الخروج من النظام"
    if request.method == "GET":
        return "PAGE_VIEW", f"فتح المستخدم الصفحة: {request.path}"
    if status_code >= 400:
        return "USER_ACTION_FAILED", f"حاول المستخدم تنفيذ إجراء ولم ينجح: {request.path}"
    return "USER_ACTION", f"نفّذ المستخدم إجراءً: {request.path}"


def register_request_audit(app) -> None:
    """Register request hooks once on the supplied Flask application."""
    if app.extensions.get("request_audit_registered"):
        return
    app.extensions["request_audit_registered"] = True

    @app.before_request
    def _capture_request_audit_actor():
        g._request_audit_started = time.perf_counter()
        try:
            if getattr(current_user, "is_authenticated", False):
                g._request_audit_user_id = int(current_user.id)
        except Exception:
            pass

    @app.after_request
    def _record_request_audit(response):
        if not _should_audit_request():
            return response

        user_id = getattr(g, "_request_audit_user_id", None)
        if not user_id:
            try:
                if getattr(current_user, "is_authenticated", False):
                    user_id = int(current_user.id)
            except Exception:
                user_id = None
        if not user_id:
            return response

        try:
            action, summary = _action_and_summary(int(response.status_code or 0))
            endpoint = request.endpoint or "غير معروف"
            rule = str(request.url_rule or request.path)
            elapsed_ms = int(max(0, (time.perf_counter() - getattr(g, "_request_audit_started", time.perf_counter())) * 1000))

            note_parts = [
                summary,
                f"الواجهة={endpoint} | الطريقة={request.method} | النتيجة={response.status_code} | المدة={elapsed_ms}ms",
                f"المسار={rule}",
            ]
            details = _request_details()
            if details:
                note_parts.append(details)
            source = get_audit_source_summary()
            if source:
                note_parts.append(source)

            request_id, target_type, target_id = _target_reference()
            on_behalf_of_id = None
            delegation_id = None
            try:
                effective_user = getattr(g, "effective_user", None)
                delegation = getattr(g, "delegation", None)
                if effective_user and int(effective_user.id) != int(user_id):
                    on_behalf_of_id = int(effective_user.id)
                    delegation_id = int(delegation.id) if delegation and getattr(delegation, "id", None) else None
            except Exception:
                on_behalf_of_id = None
                delegation_id = None

            values = {
                "request_id": request_id,
                "user_id": int(user_id),
                "on_behalf_of_id": on_behalf_of_id,
                "delegation_id": delegation_id,
                "action": action,
                "note": "\n".join(note_parts),
                "created_at": datetime.utcnow(),
                "target_type": target_type,
                "target_id": target_id,
            }
            # Use an independent transaction so auditing never commits or rolls
            # back pending domain changes from the request's ORM session.
            with db.engine.begin() as connection:
                connection.execute(AuditLog.__table__.insert().values(**values))
        except Exception:
            app.logger.exception(
                "Failed to write request audit for endpoint=%s user_id=%s",
                request.endpoint,
                user_id,
            )
        return response
