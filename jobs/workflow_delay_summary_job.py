"""In-process scheduler for the daily workflow delay summary."""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta

from extensions import db
from services.workflow_delay_summary import (
    WORKFLOW_DELAY_SUMMARY_HOUR,
    WORKFLOW_DELAY_SUMMARY_MINUTE,
    send_daily_workflow_delay_summary,
)
from utils.timezone import to_local_time


_STARTED = False
_LOCK = threading.Lock()


def _retry_interval_seconds() -> int:
    """Return the bounded delay used only after an unexpected job failure."""
    try:
        value = int(os.getenv("WORKFLOW_DELAY_SUMMARY_JOB_INTERVAL_SEC", "300"))
    except (TypeError, ValueError):
        value = 300
    return max(60, min(value, 3600))


def _seconds_until_next_run(now: datetime | None = None) -> float:
    """Return the delay until the next local daily summary slot.

    The worker calls the service once at startup.  Before 08:30 the service is
    intentionally a no-op, then the next wake-up is exactly at 08:30.  After
    a successful run the next wake-up is tomorrow, avoiding the old 30-second
    full database scan for the rest of the day.
    """
    local_now = to_local_time(now or datetime.utcnow())
    if local_now is None:
        return 60.0

    scheduled = local_now.replace(
        hour=WORKFLOW_DELAY_SUMMARY_HOUR,
        minute=WORKFLOW_DELAY_SUMMARY_MINUTE,
        second=0,
        microsecond=0,
    )
    if local_now >= scheduled:
        scheduled += timedelta(days=1)
    return max(1.0, (scheduled - local_now).total_seconds())


def _run_once(app) -> bool:
    try:
        with app.app_context():
            result = send_daily_workflow_delay_summary()
            if result.get("sent"):
                app.logger.info(
                    "Workflow delay summary sent: delayed=%s recipients=%s",
                    result.get("delayed_count", 0),
                    result.get("recipient_count", 0),
                )
        return True
    except Exception:
        app.logger.exception("Workflow delay summary job iteration failed")
        try:
            with app.app_context():
                db.session.rollback()
        except Exception:
            pass
        return False
    finally:
        try:
            with app.app_context():
                db.session.remove()
        except Exception:
            pass


def _worker(app) -> None:
    while True:
        completed = _run_once(app)
        if completed:
            # Keep the scheduler aligned with the application's configured
            # time zone, which can differ from the host machine's clock.
            with app.app_context():
                delay = _seconds_until_next_run()
        else:
            delay = _retry_interval_seconds()
        time.sleep(delay)


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
            "Workflow delay summary job started (daily schedule=%02d:%02d, retry=%ss)",
            WORKFLOW_DELAY_SUMMARY_HOUR,
            WORKFLOW_DELAY_SUMMARY_MINUTE,
            _retry_interval_seconds(),
        )
