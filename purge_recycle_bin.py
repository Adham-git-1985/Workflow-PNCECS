"""purge_recycle_bin.py

Permanently delete archive files that stayed in Recycle Bin longer than the
configured retention period.

Run:
  python purge_recycle_bin.py

Retention is read from SystemSetting key: TRASH_RETENTION_DAYS (default 30).
"""

import os
from datetime import datetime, timedelta

from app import app
from extensions import db
from models import ArchivedFile, FilePermission, RequestAttachment, AuditLog, SystemSetting


def get_trash_retention_days() -> int:
    setting = SystemSetting.query.filter_by(key="TRASH_RETENTION_DAYS").first()
    try:
        return int(setting.value) if setting and setting.value else 30
    except Exception:
        return 30


def purge_expired() -> tuple[int, int]:
    """Return (purged, skipped_attached)."""
    days = get_trash_retention_days()
    cutoff = datetime.utcnow() - timedelta(days=days)

    candidates = (
        ArchivedFile.query
        .filter(
            ArchivedFile.is_deleted.is_(True),
            ArchivedFile.deleted_at.isnot(None),
            ArchivedFile.deleted_at < cutoff,
        )
        .order_by(ArchivedFile.deleted_at.asc())
        .all()
    )

    purged = 0
    skipped = 0
    for f in candidates:
        if RequestAttachment.query.filter_by(archived_file_id=f.id).first():
            skipped += 1
            continue

        FilePermission.query.filter_by(file_id=f.id).delete(synchronize_session=False)

        try:
            if f.file_path and os.path.exists(f.file_path):
                os.remove(f.file_path)
        except Exception:
            pass

        db.session.add(
            AuditLog(
                action="ARCHIVE_PURGE",
                user_id=None,
                target_type="ARCHIVE_FILE",
                target_id=f.id,
                note=f"Auto purge: '{f.original_name}' (retention {days} days)",
            )
        )
        db.session.delete(f)
        purged += 1

    db.session.commit()
    return purged, skipped


if __name__ == "__main__":
    with app.app_context():
        p, s = purge_expired()
        print(f"Purged: {p} | Skipped (attached to requests): {s}")
