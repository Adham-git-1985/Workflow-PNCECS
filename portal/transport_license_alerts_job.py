"""Pre-expiry reminders for driver and vehicle licences."""

from __future__ import annotations

import os
import threading
import time
from datetime import date, datetime, timedelta

from sqlalchemy import inspect

from extensions import db
from models import Notification, SystemSetting, TransportDriver, TransportVehicle, User


REMINDER_DAYS = 14
_STARTED = False
_LOCK = threading.Lock()
_TRANSPORT_PERMISSION_KEYS = (
    "TRANSPORT_READ",
    "TRANSPORT_CREATE",
    "TRANSPORT_UPDATE",
    "TRANSPORT_DELETE",
    "TRANSPORT_APPROVE",
    "TRANSPORT_MANAGER_APPROVE",
    "TRANSPORT_DIRECTOR_APPROVE",
    "TRANSPORT_ADMIN_APPROVE",
)
_RESPONSIBLE_USER_SETTING_KEYS = (
    "TRANSPORT_MANAGER_USER_ID",
    "TRANSPORT_DIRECTOR_USER_ID",
    "TRANSPORT_ADMIN_USER_ID",
)


def _setting(key: str, default: str) -> str:
    row = SystemSetting.query.filter_by(key=key).first()
    return str(row.value) if row and row.value not in (None, "") else default


def _schema_ready() -> bool:
    """Do not query the new fields until a legacy deployment has been updated."""
    try:
        inspector = inspect(db.engine)
        vehicle_columns = {column["name"] for column in inspector.get_columns("transport_vehicle")}
        driver_columns = {column["name"] for column in inspector.get_columns("transport_driver")}
        return {
            "license_end_day",
            "license_alert_sent_for",
        }.issubset(vehicle_columns) and {
            "license_end_day",
            "license_alert_sent_for",
        }.issubset(driver_columns)
    except Exception:
        return False


def _configured_user_ids() -> set[int]:
    recipient_ids: set[int] = set()
    for key in _RESPONSIBLE_USER_SETTING_KEYS:
        try:
            user_id = int((_setting(key, "") or "").strip())
        except (TypeError, ValueError):
            continue
        if user_id > 0:
            recipient_ids.add(user_id)
    return recipient_ids


def _recipient_ids(extra_user_id: int | None = None) -> set[int]:
    """Notify fleet users, configured transport managers, and the linked driver."""
    recipient_ids = _configured_user_ids()
    if extra_user_id:
        recipient_ids.add(int(extra_user_id))

    for user in User.query.all():
        try:
            if any(user.has_perm(key) for key in _TRANSPORT_PERMISSION_KEYS):
                recipient_ids.add(int(user.id))
        except Exception:
            continue
    return recipient_ids


def _expiring_items(model, today_iso: str, deadline_iso: str, statuses: tuple[str, ...]):
    return (
        model.query
        .filter(
            model.status.in_(statuses),
            model.license_end_day.isnot(None),
            model.license_end_day >= today_iso,
            model.license_end_day <= deadline_iso,
            (model.license_alert_sent_for.is_(None) | (model.license_alert_sent_for != model.license_end_day)),
        )
        .all()
    )


def _add_notifications(recipient_ids: set[int], message: str, link_url: str) -> int:
    for user_id in recipient_ids:
        db.session.add(Notification(
            user_id=user_id,
            message=message[:255],
            type="TRANSPORT_LICENSE_EXPIRY",
            source="portal",
            is_read=False,
            is_mirror=False,
            created_at=datetime.utcnow(),
            link_url=link_url,
        ))
    return len(recipient_ids)


def check_transport_license_expirations(today: date | None = None) -> int:
    """Create one portal notification/email outbox entry per licence expiry.

    The reminder is emitted as soon as an expiry date enters the 14-day window.
    Each record remembers the date already alerted, so changing a renewed licence
    date automatically makes it eligible for its next reminder.
    """
    if not _schema_ready():
        return 0

    today = today or date.today()
    today_iso = today.isoformat()
    deadline_iso = (today + timedelta(days=REMINDER_DAYS)).isoformat()
    alerted_items = 0

    drivers = _expiring_items(TransportDriver, today_iso, deadline_iso, ("ACTIVE",))
    for driver in drivers:
        recipient_ids = _recipient_ids(driver.user_id)
        if not recipient_ids:
            continue
        remaining_days = (date.fromisoformat(driver.license_end_day) - today).days
        message = (
            f"تنبيه رخصة سائق: رخصة السائق {driver.name} "
            f"تنتهي بتاريخ {driver.license_end_day} (بعد {remaining_days} يومًا)."
        )
        _add_notifications(recipient_ids, message, "/portal/transport/drivers?license_state=EXPIRING")
        driver.license_alert_sent_for = driver.license_end_day
        alerted_items += 1

    vehicles = _expiring_items(TransportVehicle, today_iso, deadline_iso, ("ACTIVE", "MAINTENANCE"))
    for vehicle in vehicles:
        recipient_ids = _recipient_ids()
        if not recipient_ids:
            continue
        remaining_days = (date.fromisoformat(vehicle.license_end_day) - today).days
        vehicle_label = vehicle.plate_no or vehicle.label or f"#{vehicle.id}"
        message = (
            f"تنبيه رخصة مركبة: رخصة المركبة {vehicle_label} "
            f"تنتهي بتاريخ {vehicle.license_end_day} (بعد {remaining_days} يومًا)."
        )
        _add_notifications(recipient_ids, message, "/portal/transport/vehicles?license_state=EXPIRING")
        vehicle.license_alert_sent_for = vehicle.license_end_day
        alerted_items += 1

    db.session.commit()
    return alerted_items


def _worker(app) -> None:
    while True:
        interval = 3600
        try:
            with app.app_context():
                enabled = _setting("TRANSPORT_LICENSE_ALERTS_JOB_ENABLED", "1").strip().lower()
                if enabled in {"1", "true", "yes", "on"}:
                    check_transport_license_expirations()
                try:
                    interval = int(_setting("TRANSPORT_LICENSE_ALERTS_JOB_INTERVAL_SEC", "3600"))
                except (TypeError, ValueError):
                    interval = 3600
        except Exception:
            app.logger.exception("Transport licence alerts job iteration failed")
            try:
                with app.app_context():
                    db.session.rollback()
            except Exception:
                pass
        time.sleep(max(300, interval))


def start_transport_license_alerts_job(app) -> None:
    """Start one daemon worker per web-server process."""
    global _STARTED
    if getattr(app, "testing", False):
        return
    with _LOCK:
        if _STARTED:
            return
        if getattr(app, "debug", False):
            launched_by_flask_cli = os.environ.get("FLASK_RUN_FROM_CLI") in {"1", "true", "True"}
            if launched_by_flask_cli and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
                return
        thread = threading.Thread(
            target=_worker,
            args=(app,),
            daemon=True,
            name="TransportLicenceAlertsJob",
        )
        thread.start()
        _STARTED = True
