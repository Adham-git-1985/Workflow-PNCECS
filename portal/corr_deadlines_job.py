"""Daily deadline reminders for open correspondence."""

import os
import threading
import time
from datetime import date, datetime

from sqlalchemy import inspect

from extensions import db
from models import CorrConfidentialAccess, InboundMail, Notification, OutboundMail, SystemSetting
from utils.notification_links import notification_target_path


_STARTED = False
_LOCK = threading.Lock()
_FINAL_STATUSES = {"APPROVED", "COMPLETED", "CLOSED", "ARCHIVED"}


def _setting(key: str, default: str) -> str:
    row = SystemSetting.query.filter_by(key=key).first()
    return str(row.value) if row and row.value not in (None, "") else default


def _procedure_schema_ready() -> bool:
    """Avoid querying new columns before the deployment migration has run."""
    try:
        inspector = inspect(db.engine)
        inbound = {column["name"] for column in inspector.get_columns("corr_inbound")}
        outbound = {column["name"] for column in inspector.get_columns("corr_outbound")}
        return {"status", "due_date", "deadline_notified_on"}.issubset(inbound) and {
            "status", "due_date", "deadline_notified_on"
        }.issubset(outbound)
    except Exception:
        return False


def _recipient_ids(item) -> set[int]:
    recipients = {
        int(user_id)
        for user_id in (item.current_assignee_id, item.created_by_id)
        if user_id
    }
    try:
        from portal.routes import _corr_competence_user_ids

        recipients.update(_corr_competence_user_ids({
            "kind": item.current_target_kind or item.competence_kind,
            "id": item.current_target_id or item.competence_id,
            "label": item.current_target_label or item.competence_label,
        }))
    except Exception:
        pass

    if (getattr(item, "confidentiality", "NORMAL") or "NORMAL").upper() == "SECRET":
        parent_filter = (
            CorrConfidentialAccess.inbound_id == item.id
            if isinstance(item, InboundMail)
            else CorrConfidentialAccess.outbound_id == item.id
        )
        allowed = {
            int(row[0])
            for row in (
                db.session.query(CorrConfidentialAccess.user_id)
                .filter(parent_filter)
                .all()
            )
            if row[0]
        }
        for user_id in (item.current_assignee_id, item.created_by_id):
            if user_id:
                allowed.add(int(user_id))
        recipients.intersection_update(allowed)
    return recipients


def check_correspondence_deadlines(today: date | None = None) -> int:
    """Send at most one reminder per open item per calendar day."""
    if not _procedure_schema_ready():
        return 0

    today = today or date.today()
    today_iso = today.isoformat()
    sent = 0
    for kind_label, model in (("وارد", InboundMail), ("صادر", OutboundMail)):
        items = model.query.filter(
            model.due_date.isnot(None),
            model.due_date <= today_iso,
            model.status.notin_(_FINAL_STATUSES),
            (model.deadline_notified_on.is_(None) | (model.deadline_notified_on != today_iso)),
        ).all()
        for item in items:
            overdue = (item.due_date or "") < today_iso
            timing = "تجاوز موعده النهائي" if overdue else "يستحق الإجراء اليوم"
            message = f"تنبيه موعد: {kind_label} رقم {item.ref_no or item.id} {timing}: {item.subject[:150]}"
            target_type = "CORR_INBOUND" if isinstance(item, InboundMail) else "CORR_OUTBOUND"
            for user_id in _recipient_ids(item):
                db.session.add(Notification(
                    user_id=user_id,
                    message=message[:255],
                    type="CORR_DEADLINE",
                    source="portal",
                    is_read=False,
                    is_mirror=False,
                    created_at=datetime.utcnow(),
                    link_url=notification_target_path(target_type, item.id),
                ))
            item.deadline_notified_on = today_iso
            sent += 1
    db.session.commit()
    return sent


def _worker(app) -> None:
    while True:
        interval = 3600
        try:
            with app.app_context():
                enabled = _setting("CORR_DEADLINE_JOB_ENABLED", "1").strip().lower()
                if enabled in {"1", "true", "yes", "on"}:
                    check_correspondence_deadlines()
                try:
                    interval = int(_setting("CORR_DEADLINE_JOB_INTERVAL_SEC", "3600"))
                except (TypeError, ValueError):
                    interval = 3600
        except Exception:
            app.logger.exception("Correspondence deadline job iteration failed")
            try:
                with app.app_context():
                    db.session.rollback()
            except Exception:
                pass
        time.sleep(max(300, interval))


def start_correspondence_deadline_job(app) -> None:
    global _STARTED
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
            name="CorrespondenceDeadlineJob",
        )
        thread.start()
        _STARTED = True
