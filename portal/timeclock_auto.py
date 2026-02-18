import os
import time
import threading
from datetime import datetime

from sqlalchemy import func

from extensions import db
from models import SystemSetting, User


_started = False
_lock = threading.Lock()


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
        while True:
            try:
                file_path = _setting_get("TIMECLK_SOURCE_FILE", "")
                enabled_default = True if file_path else False
                enabled = _setting_get_bool("TIMECLK_AUTO_SYNC_ENABLED", enabled_default)
                interval = max(10, _setting_get_int("TIMECLK_AUTO_SYNC_INTERVAL", 60))
                append_only = _setting_get_bool("TIMECLK_APPEND_ONLY", True)

                # Heartbeat: show the admin that polling is alive even if there is no new data
                try:
                    _setting_set("TIMECLK_LAST_CHECK_AT", datetime.utcnow().isoformat(timespec='seconds'))
                    db.session.commit()
                except Exception:
                    db.session.rollback()

                if (not enabled) or (not file_path):
                    time.sleep(interval)
                    continue

                try:
                    from portal.routes import _timeclock_resolve_source_file  # local import
                    resolved = _timeclock_resolve_source_file(file_path)
                    if not resolved:
                        app.logger.warning("TIMECLK auto-sync: source is empty/unreachable: %s", file_path)
                        try:
                            _setting_set("TIMECLK_LAST_ERROR", "SOURCE_UNREACHABLE")
                            db.session.commit()
                        except Exception:
                            db.session.rollback()
                        time.sleep(interval)
                        continue

                    st = os.stat(resolved)
                    mtime_ns = getattr(st, 'st_mtime_ns', int(st.st_mtime * 1_000_000_000))
                    sig = (resolved, st.st_size, mtime_ns)
                except FileNotFoundError:
                    app.logger.warning("TIMECLK auto-sync: source file not found: %s", file_path)
                    try:
                        _setting_set("TIMECLK_LAST_ERROR", "SOURCE_NOT_FOUND")
                        db.session.commit()
                    except Exception:
                        db.session.rollback()
                    time.sleep(interval)
                    continue
                except Exception as e:
                    app.logger.exception("TIMECLK auto-sync: stat failed: %s", e)
                    try:
                        _setting_set("TIMECLK_LAST_ERROR", f"STAT_FAILED:{type(e).__name__}")
                        db.session.commit()
                    except Exception:
                        db.session.rollback()
                    time.sleep(interval)
                    continue

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

                    should_sync = False
                    if not stored_last_file:
                        should_sync = True
                    elif stored_last_file != sig[0]:
                        should_sync = True
                    elif append_only:
                        # Sync if file grew OR was truncated
                        if stored_last_size is None:
                            should_sync = True
                        elif sig[1] != stored_last_size:
                            should_sync = True
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
                                )
                                app.logger.info(
                                    "TIMECLK auto-sync: inserted=%s skipped=%s errors=%s source=%s",
                                    ins, skp, errs, sig[0]
                                )
                                try:
                                    _setting_set("TIMECLK_LAST_ERROR", "")
                                    db.session.commit()
                                except Exception:
                                    db.session.rollback()
                            except Exception as e:
                                app.logger.exception("TIMECLK auto-sync: sync failed: %s", e)
                                try:
                                    db.session.rollback()
                                except Exception:
                                    pass
                                try:
                                    _setting_set("TIMECLK_LAST_ERROR", f"SYNC_FAILED:{type(e).__name__}")
                                    db.session.commit()
                                except Exception:
                                    db.session.rollback()
                        else:
                            app.logger.warning("TIMECLK auto-sync: no user available for imported_by_id")
                            try:
                                _setting_set("TIMECLK_LAST_ERROR", "NO_IMPORTED_BY_USER")
                                db.session.commit()
                            except Exception:
                                db.session.rollback()
                except Exception as e:
                    app.logger.exception("TIMECLK auto-sync: decision failed: %s", e)

                last_sig = sig

                try:
                    db.session.remove()
                except Exception:
                    pass

                time.sleep(interval)

            except Exception as e:
                app.logger.exception("TIMECLK auto-sync worker crashed: %s", e)
                time.sleep(60)
