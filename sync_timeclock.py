"""Sync timeclock events from a configured server file.

Usage:
    python sync_timeclock.py

This script reads SystemSetting.TIMECLK_SOURCE_FILE and imports new attendance
events into AttendanceImportBatch/AttendanceEvent.

Recommended: run it via Windows Task Scheduler or cron every 1-5 minutes.
"""

from __future__ import annotations

import sys
from pathlib import Path

from app import app
from extensions import db
from models import User


def main() -> int:
    with app.app_context():
        # We reuse the portal logic by importing the helper directly.
        try:
            from portal.routes import _setting_get, _timeclock_sync_simple
        except Exception as e:
            print(f"ERR: cannot import portal sync helpers: {e}")
            return 2

        file_path = (_setting_get('TIMECLK_SOURCE_FILE') or '').strip()
        if not file_path:
            print('ERR: TIMECLK_SOURCE_FILE is not configured. Set it in /portal/admin/integrations')
            return 3

        append_only = (_setting_get('TIMECLK_APPEND_ONLY') or '1') == '1'

        # Use first ADMIN user as importer for audits when running headless
        admin = User.query.filter(User.role.in_(['ADMIN', 'SUPERADMIN'])).order_by(User.id.asc()).first()
        importer_id = admin.id if admin else 1

        try:
            ins, skp, errs = _timeclock_sync_simple(file_path, importer_id, append_only)
            print(f'OK: inserted={ins} skipped={skp} errors={errs}')
            return 0
        except FileNotFoundError:
            print(f'ERR: source not found (file or folder is unreachable / empty): {file_path}')
            return 4
        except Exception as e:
            db.session.rollback()
            print(f'ERR: sync failed: {e}')
            return 5


if __name__ == '__main__':
    raise SystemExit(main())
