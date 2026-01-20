import os
import sys
from datetime import datetime, timedelta

# ➕ إضافة جذر المشروع إلى PYTHONPATH
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BASE_DIR)

from app import app
from extensions import db
from models import ArchivedFile
from utils.events import emit_event


def purge_archived_files():
    with app.app_context():

        days = app.config.get("ARCHIVE_PURGE_DAYS", 30)
        cutoff_date = datetime.utcnow() - timedelta(days=days)

        files = ArchivedFile.query.filter(
            ArchivedFile.is_deleted == True,
            ArchivedFile.deleted_at <= cutoff_date
        ).all()

        if not files:
            return

        for f in files:
            #  حذف الملف من القرص
            try:
                if f.file_path and os.path.exists(f.file_path):
                    os.remove(f.file_path)
            except Exception as e:
                print(f"Failed to delete file {f.file_path}: {e}")
                continue

            #  Audit + Notification
            emit_event(
                actor_id=None,  # System
                action="ARCHIVE_PURGED",
                message=f"File '{f.original_name}' permanently deleted",
                target_type="ArchivedFile",
                target_id=f.id,
                notify_role="ADMIN",
                notif_type="CRITICAL"
            )

            db.session.delete(f)

        db.session.commit()


if __name__ == "__main__":
    purge_archived_files()
