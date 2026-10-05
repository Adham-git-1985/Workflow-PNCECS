import os
import time
import threading
from datetime import datetime

from sqlalchemy import func

from extensions import db
from models import SystemSetting, User


_started = False
_lock = threading.Lock()

# The source check can run every minute, but recording an identical heartbeat
# or unreachable-source state that often would turn those cheap checks into
# competing SQLite writes.  The live worker keeps the dashboard informative
# while emitting at most one heartbeat every 15 minutes and backs off an
# unavailable network source for five minutes.
_HEARTBEAT_WRITE_INTERVAL_SECONDS = 15 * 60
_UNAVAILABLE_SOURCE_RETRY_SECONDS = 5 * 60


def _is_invalid_sqlite_database_error(exc: BaseException) -> bool:
    """Return True when an exception chain reports an invalid SQLite database file."""
    current = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if "file is not a database" in str(current).lower():
            return True
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    return False


def _stop_for_invalid_database(app, exc: BaseException) -> None:
    """Stop this worker on DB corruption instead of retrying and flooding the logs."""
    try:
        db.session.remove()
    except Exception:
        pass
    app.logger.critical(
        "TIMECLK auto-sync stopped: workflow.db is not a valid SQLite database. "
        "Stop the Workflow server, run `python tools/repair_workflow_db.py` to inspect recovery, "
        "then run `python tools/repair_workflow_db.py --apply` to restore the newest valid backup. "
        "Original error: %s",
        exc,
    )


def _setting_get(key: str, default=None):
    row = SystemSetting.query.filter_by(key=key).first()
    if row and row.value is not None and row.value != "":
        return row.value
    return default


def _setting_set(key: str, value: str | None):
    """Upsert a SystemSetting (no auto-commit)."""
    row = SystemSetting.query.filter_by(key=key).first()
    if not row:
        row = SystemSetting(key=key, value=(value or ""))
        db.session.add(row)
    else:
        row.value = (value or "")
    return row


def _setting_text(value) -> str:
    return str(value or "").strip()


def _heartbeat_is_due(
    last_heartbeat_at: datetime | None,
    now: datetime,
) -> bool:
    if last_heartbeat_at is None:
        return True
    try:
        elapsed = (now - last_heartbeat_at).total_seconds()
    except (TypeError, ValueError):
        return True
    return elapsed >= _HEARTBEAT_WRITE_INTERVAL_SECONDS


def _record_heartbeat_if_due(
    last_heartbeat_at: datetime | None,
    now: datetime,
) -> datetime | None:
    if not _heartbeat_is_due(last_heartbeat_at, now):
        return last_heartbeat_at
    _setting_set("TIMECLK_LAST_CHECK_AT", now.isoformat(timespec="seconds"))
    db.session.commit()
    return now


def _update_last_error_if_changed(
    current_error,
    next_error,
) -> tuple[str, bool]:
    """Persist a timeclock error only when its value has changed."""
    current = _setting_text(current_error)
    target = _setting_text(next_error)
    if current == target:
        return target, False
    _setting_set("TIMECLK_LAST_ERROR", target)
    db.session.commit()
    return target, True


def _unavailable_source_delay(interval: int) -> int:
    return max(int(interval), _UNAVAILABLE_SOURCE_RETRY_SECONDS)


def _release_session() -> None:
    try:
        db.session.remove()
    except Exception:
        pass


def _setting_get_int(key: str, default: int) -> int:
    try:
        return int(str(_setting_get(key, default)).strip())
    except Exception:
        return default


def _setting_get_bool(key: str, default: bool) -> bool:
    val = _setting_get(key, None)
    if val is None:
        return default
    val = str(val).strip().lower()
    return val in {"1", "true", "yes", "y", "on"}


def _pick_imported_by_user_id() -> int | None:
    # Prefer configured user id
    raw = _setting_get("TIMECLK_IMPORTED_BY_USER_ID", None)
    if raw:
        try:
            uid = int(str(raw).strip())
            if db.session.get(User, uid):
                return uid
        except Exception:
            pass

    # Fallback: first admin
    try:
        admin = User.query.filter(func.lower(User.role) == "admin").order_by(User.id.asc()).first()
        if admin:
            return admin.id
    except Exception:
        pass

    # Last resort: first user
    user = User.query.order_by(User.id.asc()).first()
    return user.id if user else None


def start_timeclock_auto_sync(app):
    """Start a background thread that watches the configured timeclock source file and syncs on change.

    Controlled by settings (SystemSetting):
      - TIMECLK_SOURCE_FILE (str): full path
      - TIMECLK_AUTO_SYNC_ENABLED (0/1): default True if source file is set
      - TIMECLK_AUTO_SYNC_INTERVAL (seconds): default 60
      - TIMECLK_APPEND_ONLY (0/1): default True
      - TIMECLK_IMPORTED_BY_USER_ID (int): optional
    """
    global _started
    with _lock:
        if _started:
            return

        # Avoid starting twice in the Flask dev reloader (but DO start under WSGI servers even if DEBUG=True)
        if app.debug and (os.environ.get("FLASK_RUN_FROM_CLI") in {"1", "true", "True"}):
            if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
                return

        t = threading.Thread(target=_worker, args=(app,), daemon=True, name="timeclock-auto-sync")
        t.start()
        _started = True


def _worker(app):
    last_sig = None
    with app.app_context():
        try:
            last_error = _setting_text(_setting_get("TIMECLK_LAST_ERROR", ""))
        except Exception:
            last_error = ""
        last_heartbeat_at = None
        while True:
            try:
                file_path = _setting_get("TIMECLK_SOURCE_FILE", "")
                enabled_default = True if file_path else False
                enabled = _setting_get_bool("TIMECLK_AUTO_SYNC_ENABLED", enabled_default)
                interval = max(10, _setting_get_int("TIMECLK_AUTO_SYNC_INTERVAL", 60))
                append_only = _setting_get_bool("TIMECLK_APPEND_ONLY", True)

                # Keep the dashboard current without taking SQLite's single
                # writer slot every polling interval.
                try:
                    last_heartbeat_at = _record_heartbeat_if_due(
                        last_heartbeat_at,
                        datetime.utcnow(),
                    )
                except Exception:
                    db.session.rollback()

                if (not enabled) or (not file_path):
                    _release_session()
                    time.sleep(interval)
                    continue

                try:
                    from portal.routes import _timeclock_resolve_source_file  # local import
                    resolved = _timeclock_resolve_source_file(file_path)
                    if not resolved:
                        error_changed = False
                        try:
                            last_error, error_changed = _update_last_error_if_changed(
                                last_error,
                                "SOURCE_UNREACHABLE",
                            )
                        except Exception:
                            db.session.rollback()
                        if error_changed:
                            app.logger.warning(
                                "TIMECLK auto-sync: source is empty/unreachable: %s",
                                file_path,
                            )
                        _release_session()
                        time.sleep(_unavailable_source_delay(interval))
                        continue

                    st = os.stat(resolved)
                    mtime_ns = getattr(st, 'st_mtime_ns', int(st.st_mtime * 1_000_000_000))
                    sig = (resolved, st.st_size, mtime_ns)
                except FileNotFoundError:
                    error_changed = False
                    try:
                        last_error, error_changed = _update_last_error_if_changed(
                            last_error,
                            "SOURCE_NOT_FOUND",
                        )
                    except Exception:
                        db.session.rollback()
                    if error_changed:
                        app.logger.warning(
                            "TIMECLK auto-sync: source file not found: %s",
                            file_path,
                        )
                    _release_session()
                    time.sleep(_unavailable_source_delay(interval))
                    continue
                except Exception as e:
                    if _is_invalid_sqlite_database_error(e):
                        _stop_for_invalid_database(app, e)
                        return
                    error_changed = False
                    try:
                        last_error, error_changed = _update_last_error_if_changed(
                            last_error,
                            f"STAT_FAILED:{type(e).__name__}",
                        )
                    except Exception:
                        db.session.rollback()
                    if error_changed:
                        app.logger.exception("TIMECLK auto-sync: stat failed: %s", e)
                    _release_session()
                    time.sleep(_unavailable_source_delay(interval))
                    continue

                # A recovered source should clear its stale availability
                # message once, rather than leaving the admin UI in error.
                if (
                    last_error in {"SOURCE_UNREACHABLE", "SOURCE_NOT_FOUND"}
                    or last_error.startswith("STAT_FAILED:")
                ):
                    try:
                        last_error, _ = _update_last_error_if_changed(last_error, "")
                    except Exception:
                        db.session.rollback()

                # Decide if we should run a sync now:
                # - Always run on file rotation/change.
                # - Also run if the file size differs from the persisted pointer (covers app restarts).
                # - For full read mode, run on signature change.
                try:
                    stored_last_file = (_setting_get("TIMECLK_LAST_FILE", "") or "").strip()
                    stored_last_size_raw = _setting_get("TIMECLK_LAST_SIZE", None)
                    stored_last_size = None
                    if stored_last_size_raw is not None:
                        try:
                            stored_last_size = int(str(stored_last_size_raw).strip())
                        except Exception:
                            stored_last_size = None
                    stored_last_mtime_raw = _setting_get("TIMECLK_LAST_MTIME_NS", None)
                    stored_last_mtime = None
                    if stored_last_mtime_raw is not None:
                        try:
                            stored_last_mtime = int(str(stored_last_mtime_raw).strip())
                        except Exception:
                            stored_last_mtime = None

                    should_sync = False
                    force_full_read = False
                    if not stored_last_file:
                        should_sync = True
                    elif stored_last_file != sig[0]:
                        should_sync = True
                    elif append_only:
                        # Daily clock exports are sometimes rewritten in place
                        # instead of appended.  A same-size rewrite used to
                        # leave newly corrected exit punches unimported.
                        if stored_last_size is None:
                            should_sync = True
                            force_full_read = True
                        elif sig[1] != stored_last_size:
                            should_sync = True
                        elif stored_last_mtime is None or sig[2] != stored_last_mtime:
                            should_sync = True
                            force_full_read = True
                    else:
                        if last_sig is None or sig != last_sig:
                            should_sync = True

                    if should_sync:
                        imported_by_id = _pick_imported_by_user_id()
                        if imported_by_id:
                            from portal.routes import _timeclock_sync_simple  # local import to avoid circulars
                            try:
                                ins, skp, errs = _timeclock_sync_simple(
                                    file_path,
                                    imported_by_id=imported_by_id,
                                    append_only=append_only,
                                    force_full_read=force_full_read,
                                )
                                app.logger.info(
                                    "TIMECLK auto-sync: inserted=%s skipped=%s errors=%s source=%s",
                                    ins, skp, errs, sig[0]
                                )
                                try:
                                    if last_error:
                                        _setting_set("TIMECLK_LAST_ERROR", "")
                                    _setting_set("TIMECLK_LAST_MTIME_NS", str(sig[2]))
                                    db.session.commit()
                                    last_error = ""
                                except Exception:
                                    db.session.rollback()
                            except Exception as e:
                                if _is_invalid_sqlite_database_error(e):
                                    _stop_for_invalid_database(app, e)
                                    return
                                try:
                                    db.session.rollback()
                                except Exception:
                                    pass
                                error_changed = False
                                try:
                                    last_error, error_changed = _update_last_error_if_changed(
                                        last_error,
                                        f"SYNC_FAILED:{type(e).__name__}",
                                    )
                                except Exception:
                                    db.session.rollback()
                                if error_changed:
                                    app.logger.exception("TIMECLK auto-sync: sync failed: %s", e)
                        else:
                            error_changed = False
                            try:
                                last_error, error_changed = _update_last_error_if_changed(
                                    last_error,
                                    "NO_IMPORTED_BY_USER",
                                )
                            except Exception:
                                db.session.rollback()
                            if error_changed:
                                app.logger.warning("TIMECLK auto-sync: no user available for imported_by_id")
                except Exception as e:
                    if _is_invalid_sqlite_database_error(e):
                        _stop_for_invalid_database(app, e)
                        return
                    app.logger.exception("TIMECLK auto-sync: decision failed: %s", e)

                last_sig = sig

                _release_session()

                time.sleep(interval)

            except Exception as e:
                if _is_invalid_sqlite_database_error(e):
                    _stop_for_invalid_database(app, e)
                    return
                app.logger.exception("TIMECLK auto-sync worker crashed: %s", e)
                time.sleep(60)
