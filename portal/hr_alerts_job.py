import os
import threading
import time

from extensions import db
from portal.routes import _setting_get  # reuse SystemSetting helper (SystemSetting table)
from services.hr_request_workflow import process_pending_approvals

_HR_ALERTS_STARTED = False

def _check_pending_leave_requests():
    result = process_pending_approvals(send_notifications=True)
    db.session.commit()
    return int(result.get("reminded", 0)) + int(result.get("escalated", 0))


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
