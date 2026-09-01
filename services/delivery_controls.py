"""Global super-admin controls for notification and email delivery."""

from __future__ import annotations

from sqlalchemy import select, update

from extensions import db


# These settings deliberately default to enabled.  Existing deployments keep
# their current behaviour until a super administrator explicitly changes one.
NOTIFICATIONS_ENABLED_SETTING = "SYSTEM_NOTIFICATIONS_ENABLED"
EMAIL_DELIVERY_ENABLED_SETTING = "SYSTEM_EMAIL_DELIVERY_ENABLED"

PENDING = "PENDING"
CANCELLED = "CANCELLED"
EMAIL_DISABLED_REASON = "Email delivery was disabled by a super administrator."


def _is_enabled(value: str | None, *, default: bool = True) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _setting_value(key: str, *, session=None, connection=None) -> str | None:
    """Read a setting while also respecting unflushed changes in ``session``."""
    from models import SystemSetting

    if connection is not None:
        try:
            return connection.execute(
                select(SystemSetting.value).where(SystemSetting.key == key)
            ).scalar_one_or_none()
        except Exception:
            return None

    active_session = session or db.session
    try:
        for row in tuple(active_session.new) + tuple(active_session.dirty):
            if isinstance(row, SystemSetting) and row.key == key:
                return row.value
        return active_session.execute(
            select(SystemSetting.value).where(SystemSetting.key == key)
        ).scalar_one_or_none()
    except Exception:
        # Delivery controls must never make the application unavailable during
        # a schema upgrade or a transient database error.
        return None


def notifications_enabled(*, session=None) -> bool:
    return _is_enabled(
        _setting_value(NOTIFICATIONS_ENABLED_SETTING, session=session),
        default=True,
    )


def email_delivery_enabled(*, session=None, connection=None) -> bool:
    return _is_enabled(
        _setting_value(
            EMAIL_DELIVERY_ENABLED_SETTING,
            session=session,
            connection=connection,
        ),
        default=True,
    )


def cancel_pending_email_deliveries(*, session=None) -> int:
    """Cancel unsent outbox rows without contacting an SMTP server."""
    from models import NotificationEmailDelivery, WorkflowTaskEmailDelivery

    active_session = session or db.session
    updated = 0
    for model in (NotificationEmailDelivery, WorkflowTaskEmailDelivery):
        result = active_session.execute(
            update(model)
            .where(model.status == PENDING)
            .values(
                status=CANCELLED,
                next_attempt_at=None,
                last_error=EMAIL_DISABLED_REASON,
            )
        )
        updated += int(result.rowcount or 0)
    return updated
