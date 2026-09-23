"""In-process scheduler for the daily workflow delay summary."""

from __future__ import annotations

import os
import threading
import time

from extensions import db
from services.workflow_delay_summary import send_daily_workflow_delay_summary


_STARTED = False
_LOCK = threading.Lock()


def _interval_seconds() -> int:
    try:
        value = int(os.getenv("WORKFLOW_DELAY_SUMMARY_JOB_INTERVAL_SEC", "30"))
    except (TypeError, ValueError):
        value = 30
    return max(15, min(value, 3600))


def _run_once(app) -> None:
    try:
        with app.app_context():
            result = send_daily_workflow_delay_summary()
            if result.get("sent"):
                app.logger.info(
                    "Workflow delay summary sent: delayed=%s recipients=%s",
                    result.get("delayed_count", 0),
                    result.get("recipient_count", 0),
                )
    except Exception:
        app.logger.exception("Workflow delay summary job iteration failed")
        try:
            with app.app_context():
                db.session.rollback()
        except Exception:
            pass
    finally:
        try:
            with app.app_context():
                db.session.remove()
        except Exception:
            pass


def _worker(app) -> None:
    while True:
        _run_once(app)
        time.sleep(_interval_seconds())


def start_workflow_delay_summary_job(app) -> None:
    """Start the once-per-process poller used by the web application."""
    global _STARTED

    if getattr(app, "testing", False):
        return

    # Avoid starting twice under Flask's development reloader.
    if getattr(app, "debug", False):
        launched_by_flask_cli = os.environ.get("FLASK_RUN_FROM_CLI") in {
            "1",
            "true",
            "True",
        }
        if launched_by_flask_cli and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
            return

    with _LOCK:
        if _STARTED:
            return
        thread = threading.Thread(
            target=_worker,
            args=(app,),
            daemon=True,
            name="WorkflowDelaySummaryJob",
        )
        thread.start()
        _STARTED = True
        app.logger.info(
            "Workflow delay summary job started (schedule=08:30, interval=%ss)",
            _interval_seconds(),
        )
