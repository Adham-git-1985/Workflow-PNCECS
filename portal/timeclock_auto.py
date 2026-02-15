import os
import time
import threading

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
            if User.query.get(uid):
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

        # Avoid starting twice in Flask reloader
        if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
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

                if (not enabled) or (not file_path):
                    time.sleep(interval)
                    continue

                try:
                    from portal.routes import _timeclock_resolve_source_file  # local import
                    resolved = _timeclock_resolve_source_file(file_path)
                    if not resolved:
                        app.logger.warning("TIMECLK auto-sync: source is empty/unreachable: %s", file_path)
                        time.sleep(interval)
                        continue

                    st = os.stat(resolved)
                    sig = (resolved, st.st_size, int(st.st_mtime))
                except FileNotFoundError:
                    app.logger.warning("TIMECLK auto-sync: source file not found: %s", file_path)
                    time.sleep(interval)
                    continue
                except Exception as e:
                    app.logger.exception("TIMECLK auto-sync: stat failed: %s", e)
                    time.sleep(interval)
                    continue

                if last_sig is None:
                    last_sig = sig
                    time.sleep(interval)
                    continue

                if sig != last_sig:
                    imported_by_id = _pick_imported_by_user_id()
                    if imported_by_id:
                        from portal.routes import _timeclock_sync_simple  # local import to avoid circulars

                        ins, skp, errs = _timeclock_sync_simple(
                            file_path,
                            imported_by_id=imported_by_id,
                            append_only=append_only,
                        )
                        app.logger.info(
                            "TIMECLK auto-sync: inserted=%s skipped=%s errors=%s source=%s",
                            ins, skp, errs, sig[0]
                        )
                    else:
                        app.logger.warning("TIMECLK auto-sync: no user available for imported_by_id")

                    last_sig = sig

                time.sleep(interval)

            except Exception as e:
                app.logger.exception("TIMECLK auto-sync worker crashed: %s", e)
                time.sleep(60)
