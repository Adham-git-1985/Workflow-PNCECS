import os
import threading
import time
from datetime import datetime, timedelta

from extensions import db
from models import HRLeaveRequest, User, UserPermission, Notification
from portal.routes import _setting_get  # reuse SystemSetting helper (SystemSetting table)

_HR_ALERTS_STARTED = False


def _hr_admin_user_ids():
    # Users who can view all HR requests / approve
    keys = {"HR_REQUESTS_VIEW_ALL", "HR_REQUESTS_APPROVE"}
    ids = {
        r.user_id
        for r in UserPermission.query.filter(
            UserPermission.key.in_(list(keys)), UserPermission.is_allowed.is_(True)
        ).all()
    }
    # Any SUPER* user should also get it
    for u in User.query.all():
        role = (u.role or "").upper().replace("-", "_").replace(" ", "_")
        if role.startswith("SUPER"):
            ids.add(u.id)
    return sorted(ids)


def _notify(user_ids, msg, ntype="HR_ALERT"):
    now = datetime.utcnow()
    for uid in set(int(x) for x in user_ids if x):
        db.session.add(Notification(user_id=uid, type=ntype, message=msg, source="portal", created_at=now))


def _check_pending_leave_requests():
    # Days threshold (match portal/routes.py key)
    days = int((_setting_get("HR_ALERT_PENDING_DAYS") or "2").strip() or 2)
    # Prefer submitted_at when available; otherwise fallback to created_at
    min_created = datetime.utcnow() - timedelta(days=days)

    # Avoid spamming: send at most once per 24h per request
    cooldown = datetime.utcnow() - timedelta(hours=24)

    q = HRLeaveRequest.query.filter(
        HRLeaveRequest.status == "SUBMITTED",
        ((HRLeaveRequest.submitted_at.is_(None)) & (HRLeaveRequest.created_at <= min_created))
        | (HRLeaveRequest.submitted_at.isnot(None) & (HRLeaveRequest.submitted_at <= min_created)),
        ((HRLeaveRequest.reminder_sent_at.is_(None)) | (HRLeaveRequest.reminder_sent_at <= cooldown)),
    )

    reqs = q.order_by(HRLeaveRequest.created_at.asc()).all()
    if not reqs:
        return 0

    hr_ids = _hr_admin_user_ids()
    sent = 0

    for r in reqs:
        # Message
        emp = (r.user.name or r.user.email) if r.user else f"#{r.user_id}"
        msg = (
            f"تنبيه: طلب إجازة رقم {r.id} ({emp}) بتاريخ {r.start_date} - {r.end_date} "
            f"لم يتم اتخاذ إجراء عليه منذ أكثر من {days} يوم/أيام."
        )

        recipients = set(hr_ids)
        if r.approver_user_id:
            recipients.add(r.approver_user_id)

        _notify(recipients, msg)
        r.reminder_sent_at = datetime.utcnow()
        r.reminder_count = int(r.reminder_count or 0) + 1
        sent += 1

    db.session.commit()
    return sent


def _worker(app):
    while True:
        try:
            with app.app_context():
                enabled = (_setting_get("HR_ALERTS_JOB_ENABLED") or "1").strip()
                if enabled in ("1", "true", "True", "yes", "YES"):
                    _check_pending_leave_requests()
        except Exception:
            # Never crash the thread; errors will be visible in app logs
            try:
                db.session.rollback()
            except Exception:
                pass
        interval = int((_setting_get("HR_ALERTS_JOB_INTERVAL_SEC") or "3600").strip() or 3600)
        time.sleep(max(60, interval))


def start_hr_alerts_job(app):
    global _HR_ALERTS_STARTED

    # Avoid starting twice under Flask reloader (debug mode)
    try:
        if getattr(app, "debug", False) and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
            return
    except Exception:
        pass

    if _HR_ALERTS_STARTED:
        return

    t = threading.Thread(target=_worker, args=(app,), daemon=True, name="HRAlertsJob")
    t.start()
    _HR_ALERTS_STARTED = True
