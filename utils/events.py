from datetime import datetime
import uuid
from extensions import db
from models import Notification, User
from utils.notification_links import notification_target_path, safe_local_notification_url


def _public_delegated_actor(actor_id, message):
    """Return the public actor identity for an in-request delegated action.

    Notifications do not have the full audit identity columns, so exposing the
    login account here would disclose the delegate to recipients.  The durable
    AuditLog still keeps that technical account separately.
    """
    try:
        from flask import g, has_request_context
        from flask_login import current_user
        from utils.permissions import (
            get_effective_user,
            is_delegated_identity_selected,
        )

        if not has_request_context() or not is_delegated_identity_selected():
            return actor_id, message
        actual = getattr(g, "actual_user", None) or current_user
        actual_id = int(getattr(g, "actual_user_id", None) or getattr(actual, "id", 0) or 0)
        if not actual_id or int(actor_id or 0) != actual_id:
            return actor_id, message

        principal = get_effective_user()
        principal_id = int(getattr(principal, "id", 0) or 0)
        if not principal_id or principal_id == actual_id:
            return actor_id, message

        public_message = str(message or "")
        actual_values = (
            getattr(actual, "full_name", None),
            getattr(actual, "name", None),
            getattr(actual, "username", None),
            getattr(actual, "email", None),
        )
        principal_label = (
            getattr(principal, "full_name", None)
            or getattr(principal, "name", None)
            or getattr(principal, "username", None)
            or getattr(principal, "email", None)
            or ""
        )
        if principal_label:
            for value in actual_values:
                value = str(value or "").strip()
                if value:
                    public_message = public_message.replace(value, principal_label)
        return principal_id, public_message
    except Exception:
        return actor_id, message


def emit_event(
    actor_id,
    action,
    message,
    target_type=None,
    target_id=None,
    notify_user_id=None,
    notify_role=None,
    level="INFO",
    notif_type=None,     # alias قديم
    track_for_actor=False,  # ✅ read-receipt style tracking for sender
    auto_commit=True,    # ✅ تحكم بالـ commit
    **kwargs
):
    # لو حد استعمل notif_type بالغلط، اعتبرها level
    if notif_type is not None:
        level = notif_type

    technical_actor_id = actor_id
    actor_id, message = _public_delegated_actor(actor_id, message)

    now = datetime.utcnow()
    event_key = uuid.uuid4().hex

    # ✅ منع التكرار (مثلاً notify_user_id ضمن نفس الدور)
    user_ids = set()

    if notify_user_id:
        user_ids.add(int(notify_user_id))

    if notify_role:
        role_user_ids = (
            db.session.query(User.id)
            .filter(User.role == notify_role)
            .all()
        )
        for (uid,) in role_user_ids:
            user_ids.add(int(uid))

    if not user_ids:
        return

    notifications = []
    source = str(kwargs.get("source") or "workflow").strip().lower()
    if source not in {"workflow", "portal"}:
        source = "workflow"
    link_url = safe_local_notification_url(kwargs.get("link_url"))
    if not link_url:
        link_url = notification_target_path(target_type, target_id)

    # Recipient notifications
    for uid in user_ids:
        notifications.append(
            Notification(
                user_id=uid,
                message=message,
                type=level,
                is_read=False,
                created_at=now,
                actor_id=actor_id,
                event_key=event_key,
                is_mirror=False,
                link_url=link_url,
                source=source,
            )
        )

    # Sender mirror notification (shows "unread" until recipients read)
    mirror_user_id = technical_actor_id or actor_id
    if track_for_actor and mirror_user_id and int(mirror_user_id) not in user_ids:
        notifications.append(
            Notification(
                user_id=int(mirror_user_id),
                message=f"متابعة: {message}",
                type=level,
                is_read=False,
                created_at=now,
                actor_id=int(actor_id),
                event_key=event_key,
                is_mirror=True,
                link_url=link_url,
                source=source,
            )
        )

    db.session.add_all(notifications)

    if auto_commit:
        db.session.commit()
