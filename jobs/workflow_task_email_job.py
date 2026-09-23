"""Background worker for workflow task assignment emails and reminders."""

import os
import threading
import time

from services.workflow_task_email import run_workflow_task_email_cycle
from services.notification_email import send_pending_notification_emails
from services.attendance_report_email import run_attendance_report_email_cycle


_STARTED = False
_LOCK = threading.Lock()


def _interval_seconds() -> int:
    try:
        value = int(os.getenv("WORKFLOW_TASK_EMAIL_JOB_INTERVAL_SEC", "60"))
    except (TypeError, ValueError):
        value = 60
    return max(60, min(value, 3600))


def _run_job_step(app, name: str, callback):
    """Run one email-related step without starving the other mail services.

    The workflow and notification outboxes use models that may be ahead of a
    long-lived SQLite database during a rolling deployment.  A failure in one
    of those queries must not prevent the independent HR attendance report
    from being evaluated during the same poll.
    """
    try:
        with app.app_context():
            result = callback()
            if name == "attendance report" and isinstance(result, dict):
                status = result.get("status")
                if status in {"sent", "failed"}:
                    if status == "failed":
                        app.logger.error(
                            "Attendance report email failed: %s",
                            result.get("error") or "unknown error",
                        )
                    else:
                        app.logger.info(
                            "Attendance report email sent: run_date=%s recipients=%s",
                            result.get("run_date"),
                            result.get("recipients", 0),
                        )
            return result
    except Exception:
        app.logger.exception("%s email job step failed", name)
        try:
            with app.app_context():
                from extensions import db

                db.session.rollback()
        except Exception:
            pass
        return None
    finally:
        # A failed SQLAlchemy query can leave a scoped session in a failed
        # transaction.  Remove it before the next independent step.
        try:
            with app.app_context():
                from extensions import db

                db.session.remove()
        except Exception:
            pass


def _worker(app) -> None:
    while True:
        _run_job_step(app, "workflow task", run_workflow_task_email_cycle)
        _run_job_step(app, "notification", send_pending_notification_emails)
        _run_job_step(app, "attendance report", run_attendance_report_email_cycle)
        time.sleep(_interval_seconds())


def start_workflow_task_email_job(app) -> None:
    """Start one daemon email worker per web-server process."""
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
            name="WorkflowTaskEmailJob",
        )
        thread.start()
        _STARTED = True
        app.logger.info("Workflow task email job started (interval=%ss)", _interval_seconds())
