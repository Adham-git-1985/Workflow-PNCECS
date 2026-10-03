"""Background polling worker for private calendar reminders."""

from __future__ import annotations

import os
import threading
import time

from extensions import db
from services.user_calendar_reminders import send_due_user_calendar_reminders


_STARTED = False
_LOCK = threading.Lock()


def _interval_seconds() -> int:
    try:
        value = int(os.getenv("USER_CALENDAR_REMINDER_JOB_INTERVAL_SEC", "60"))
    except (TypeError, ValueError):
        value = 60
    return max(60, min(value, 3600))


def _run_once(app) -> None:
    try:
        with app.app_context():
            emitted = send_due_user_calendar_reminders()
            if emitted:
                app.logger.info("User calendar reminders emitted: %s", emitted)
    except Exception:
        app.logger.exception("User calendar reminder job iteration failed")
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


def start_user_calendar_reminder_job(app) -> None:
    """Start one reminder worker per web-server process."""
    global _STARTED
    if getattr(app, "testing", False):
        return
    with _LOCK:
        if _STARTED:
            return
        if getattr(app, "debug", False):
            launched_by_flask_cli = os.environ.get("FLASK_RUN_FROM_CLI") in {
                "1",
                "true",
                "True",
            }
            if launched_by_flask_cli and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
                return
        thread = threading.Thread(
            target=_worker,
            args=(app,),
            daemon=True,
            name="UserCalendarReminderJob",
        )
        thread.start()
        _STARTED = True
        app.logger.info(
            "User calendar reminder job started (interval=%ss)",
            _interval_seconds(),
        )
