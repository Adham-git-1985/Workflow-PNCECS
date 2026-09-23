import os
import threading
import time

from sqlalchemy.exc import SQLAlchemyError

from extensions import db
from portal.routes import (  # reuse leave helpers and SystemSetting table
    _activate_due_leave_rollover_decisions,
    _setting_get,
)
from services.attendance_schedule import send_attendance_schedule_reminders
from services.attendance_delay_workflow import process_pending_delay_response_alerts
from services.hr_request_workflow import process_pending_approvals

_HR_ALERTS_STARTED = False

def _check_pending_leave_requests():
    rollover_activations = 0
    delay_overdue_alerts = 0
    try:
        rollover_activations = len(_activate_due_leave_rollover_decisions())
    except Exception:
        # Do not let a legacy database that has not yet received the new table
        # prevent the rest of the HR reminders from running.
        db.session.rollback()
    result = process_pending_approvals(send_notifications=True)
    try:
        delay_overdue_alerts = process_pending_delay_response_alerts()
    except Exception as error:
        # A missing/partially migrated delay table must not stop the existing
        # HR approval reminders from running.
        # Only a real SQLAlchemy failure leaves the transaction unusable.  The
        # narrower handling also keeps lightweight test/dry-run sessions from
        # being treated as failed database transactions.
        if isinstance(error, SQLAlchemyError):
            db.session.rollback()
    followup_reminders = 0
    schedule_reminders = 0
    automatic_attendance_leaves = 0
    try:
        from portal.followups import send_followup_reminders

        enabled = (_setting_get("FOLLOWUPS_ALERTS_JOB_ENABLED", "1") or "1").strip().lower()
        if enabled in {"1", "true", "yes", "on"}:
            followup_reminders = send_followup_reminders()
    except Exception:
        # Reporting reminders must not prevent employee-request reminders.
        pass
    try:
        schedule_reminders = send_attendance_schedule_reminders()
    except Exception:
        pass
    try:
        # Reconcile completed office-duty days after the configured cutoff.
        # The reconciliation creates an approved annual-leave request, so the
        # normal balance calculation deducts the day and the attendance views
        # render it as approved leave.  The helper is idempotent and releases
        # the charge if a late punch or an approved correction appears later.
        from portal.routes import _process_unrecorded_office_attendance

        attendance_result = _process_unrecorded_office_attendance()
        automatic_attendance_leaves = int(attendance_result.get("created", 0))
    except Exception:
        # Keep the pending-approval and attendance changes atomic.  A failed
        # attendance reconciliation must not commit partial HR updates.
        try:
            db.session.rollback()
        except Exception:
            pass
        raise
    db.session.commit()
    return (
        int(result.get("reminded", 0))
        + int(result.get("escalated", 0))
        + int(rollover_activations)
        + int(followup_reminders)
        + int(schedule_reminders)
        + int(automatic_attendance_leaves)
        + int(delay_overdue_alerts)
    )


def _worker(app):
    while True:
        interval = 3600
        try:
            with app.app_context():
                enabled = (_setting_get("HR_ALERTS_JOB_ENABLED") or "1").strip()
                if enabled in ("1", "true", "True", "yes", "YES"):
                    _check_pending_leave_requests()
                interval = int(
                    (_setting_get("HR_ALERTS_JOB_INTERVAL_SEC") or "3600").strip()
                    or 3600
                )
                delay_interval = int(
                    (_setting_get("ATTENDANCE_DELAY_ALERT_INTERVAL_SEC") or "60").strip()
                    or 60
                )
                interval = min(interval, max(60, delay_interval))
        except Exception:
            app.logger.exception("HR alerts job iteration failed")
            try:
                with app.app_context():
                    db.session.rollback()
            except Exception:
                pass
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
