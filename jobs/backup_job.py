"""Automatic full-system backups with missed-run recovery."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Callable


_STARTED = False
_START_LOCK = threading.Lock()
_STATE_FILENAME = "automatic_backup_state.json"


def _is_enabled(value) -> bool:
    return str(value if value is not None else "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _schedule_parts(app) -> tuple[int, int]:
    try:
        hour = int(app.config.get("AUTO_BACKUP_HOUR", 15))
    except (TypeError, ValueError):
        hour = 15
    try:
        minute = int(app.config.get("AUTO_BACKUP_MINUTE", 0))
    except (TypeError, ValueError):
        minute = 0
    return min(max(hour, 0), 23), min(max(minute, 0), 59)


def latest_due_date(now: datetime, hour: int = 15, minute: int = 0) -> date:
    """Return the calendar date of the latest scheduled run that should exist."""
    scheduled_today = datetime.combine(now.date(), clock_time(hour=hour, minute=minute))
    if now >= scheduled_today:
        return now.date()
    return now.date() - timedelta(days=1)


def backup_is_due(last_successful_date: date | None, scheduled_for: date) -> bool:
    return last_successful_date is None or last_successful_date < scheduled_for


def _state_path(app) -> str:
    return os.path.join(app.instance_path, _STATE_FILENAME)


def _read_last_successful_date(app) -> date | None:
    try:
        with open(_state_path(app), "r", encoding="utf-8") as state_file:
            payload = json.load(state_file)
        return date.fromisoformat(payload["last_successful_schedule_date"])
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _write_success_state(app, scheduled_for: date, backup_path: str, completed_at: datetime) -> None:
    os.makedirs(app.instance_path, exist_ok=True)
    state_path = _state_path(app)
    temporary_path = f"{state_path}.{uuid.uuid4().hex}.tmp"
    payload = {
        "last_successful_schedule_date": scheduled_for.isoformat(),
        "completed_at_local": completed_at.isoformat(timespec="seconds"),
        "backup_path": os.path.abspath(backup_path),
    }
    try:
        with open(temporary_path, "w", encoding="utf-8") as state_file:
            json.dump(payload, state_file, ensure_ascii=False, indent=2)
        os.replace(temporary_path, state_path)
    finally:
        if os.path.exists(temporary_path):
            try:
                os.remove(temporary_path)
            except OSError:
                pass


def _windows_desktop_path() -> str | None:
    if os.name != "nt":
        return None
    try:
        import winreg

        registry_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry_path) as key:
            value, _value_type = winreg.QueryValueEx(key, "Desktop")
        return os.path.expandvars(str(value))
    except (ImportError, OSError, TypeError):
        return None


def _xdg_desktop_path() -> str | None:
    """Resolve a Linux desktop path without assuming its localized folder name."""
    if os.name == "nt":
        return None
    try:
        config_path = Path.home() / ".config" / "user-dirs.dirs"
        with config_path.open("r", encoding="utf-8") as config_file:
            for line in config_file:
                key, separator, raw_value = line.partition("=")
                if separator and key.strip() == "XDG_DESKTOP_DIR":
                    value = raw_value.strip().strip('"').replace("$HOME", str(Path.home()))
                    return os.path.expandvars(os.path.expanduser(value))
    except (OSError, UnicodeError):
        return None
    return None


def backup_destination_dir(app) -> str:
    configured = str(app.config.get("AUTO_BACKUP_DIR") or "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(os.path.expandvars(configured)))

    desktop = _windows_desktop_path() or _xdg_desktop_path() or str(Path.home() / "Desktop")
    return os.path.abspath(desktop)


def _unique_destination_path(destination_dir: str, filename: str) -> str:
    candidate = os.path.join(destination_dir, filename)
    if not os.path.exists(candidate):
        return candidate

    stem, extension = os.path.splitext(filename)
    for number in range(2, 10_000):
        candidate = os.path.join(destination_dir, f"{stem}_{number}{extension}")
        if not os.path.exists(candidate):
            return candidate
    raise FileExistsError(f"Could not choose a unique backup filename in {destination_dir}")


def _cleanup_runtime_backup(app, archive_path: str) -> None:
    archive_parent = os.path.abspath(os.path.dirname(archive_path))
    runtime_tmp = os.path.abspath(os.path.join(app.instance_path, "tmp"))
    try:
        is_runtime_path = os.path.commonpath([runtime_tmp, archive_parent]) == runtime_tmp
    except ValueError:
        is_runtime_path = False
    if is_runtime_path and archive_parent != runtime_tmp:
        shutil.rmtree(archive_parent, ignore_errors=True)


def create_automatic_backup(
    app,
    scheduled_for: date,
    *,
    completed_at: datetime | None = None,
    build_backup: Callable[[], str] | None = None,
) -> str:
    """Build the same ZIP as the manual backup and copy it atomically to Desktop."""
    completed_at = completed_at or datetime.now()
    source_path = ""
    partial_path = ""

    with app.app_context():
        if build_backup is None:
            from admin.routes import _build_backup_zip

            build_backup = _build_backup_zip

        try:
            source_path = os.path.abspath(build_backup())
            destination_dir = backup_destination_dir(app)
            os.makedirs(destination_dir, exist_ok=True)
            destination_path = _unique_destination_path(
                destination_dir,
                os.path.basename(source_path),
            )
            partial_path = f"{destination_path}.{uuid.uuid4().hex}.partial"
            shutil.copy2(source_path, partial_path)
            os.replace(partial_path, destination_path)
            partial_path = ""
            _write_success_state(app, scheduled_for, destination_path, completed_at)
            app.logger.info(
                "Automatic backup completed for %s: %s",
                scheduled_for.isoformat(),
                destination_path,
            )
            return destination_path
        finally:
            if partial_path and os.path.exists(partial_path):
                try:
                    os.remove(partial_path)
                except OSError:
                    pass
            if source_path:
                _cleanup_runtime_backup(app, source_path)


def _seconds_until_next_schedule(now: datetime, hour: int, minute: int) -> float:
    next_schedule = datetime.combine(now.date(), clock_time(hour=hour, minute=minute))
    if next_schedule <= now:
        next_schedule += timedelta(days=1)
    return max(1.0, (next_schedule - now).total_seconds())


def _worker(app) -> None:
    while True:
        delay = 60.0
        try:
            if not _is_enabled(app.config.get("AUTO_BACKUP_ENABLED", "1")):
                delay = 300.0
            else:
                now = datetime.now()
                hour, minute = _schedule_parts(app)
                scheduled_for = latest_due_date(now, hour, minute)
                last_successful = _read_last_successful_date(app)
                if backup_is_due(last_successful, scheduled_for):
                    create_automatic_backup(
                        app,
                        scheduled_for,
                        completed_at=now,
                    )
                delay = min(60.0, _seconds_until_next_schedule(datetime.now(), hour, minute))
        except Exception:
            app.logger.exception("Automatic backup job iteration failed")
            delay = 300.0
        time.sleep(max(1.0, delay))


def start_automatic_backup_job(app) -> None:
    """Start one daemon worker per process; safe to call from multiple entry points."""
    global _STARTED

    if getattr(app, "testing", False):
        return

    if not _is_enabled(app.config.get("AUTO_BACKUP_ENABLED", "1")):
        app.logger.info("Automatic backup job is disabled")
        return

    with _START_LOCK:
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
            name="AutomaticBackupJob",
        )
        thread.start()
        _STARTED = True
        hour, minute = _schedule_parts(app)
        app.logger.info(
            "Automatic backup job started (daily at %02d:%02d, destination=%s)",
            hour,
            minute,
            backup_destination_dir(app),
        )
