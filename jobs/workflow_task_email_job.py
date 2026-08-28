"""Background worker for workflow task assignment emails and reminders."""

import os
import threading
import time

from services.workflow_task_email import run_workflow_task_email_cycle


_STARTED = False
_LOCK = threading.Lock()


def _interval_seconds() -> int:
    try:
        value = int(os.getenv("WORKFLOW_TASK_EMAIL_JOB_INTERVAL_SEC", "60"))
    except (TypeError, ValueError):
        value = 60
    return max(60, min(value, 3600))


def _worker(app) -> None:
    while True:
        try:
            with app.app_context():
                run_workflow_task_email_cycle()
        except Exception:
            app.logger.exception("Workflow task email job iteration failed")
            try:
                with app.app_context():
                    from extensions import db

                    db.session.rollback()
            except Exception:
                pass
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
