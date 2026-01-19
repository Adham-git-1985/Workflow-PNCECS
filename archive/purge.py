import os
from datetime import datetime, timedelta
from extensions import db
from models import ArchivedFile, AuditLog

def purge_archived_files(days):
    cutoff = datetime.utcnow() - timedelta(days=days)

    files = ArchivedFile.query.filter(
        ArchivedFile.is_deleted == True,
        ArchivedFile.deleted_at <= cutoff
    ).all()

    for f in files:
        # حذف من القرص
        try:
            if os.path.exists(f.file_path):
                os.remove(f.file_path)
        except Exception as e:
            print(e)

        # Audit
        log = AuditLog(
            action="ARCHIVE_PURGE",
            user_id=None,
            target_type="ArchivedFile",
            target_id=f.id,
            description=f"Permanently deleted file '{f.original_name}'"
        )
        db.session.add(log)

        db.session.delete(f)

    db.session.commit()
