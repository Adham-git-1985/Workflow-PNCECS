from datetime import datetime, timedelta
from extensions import db
from models import ArchivedFile, AuditLog, FilePermission, RequestAttachment

def purge_archived_files(days):
    """Move expired recycle-bin files to Super Trash instead of deleting them."""
    cutoff = datetime.utcnow() - timedelta(days=days)

    files = ArchivedFile.query.filter(
        ArchivedFile.is_deleted == True,
        (ArchivedFile.is_final_deleted == False) | (ArchivedFile.is_final_deleted == None),
        ArchivedFile.deleted_at <= cutoff
    ).all()

    moved = 0
    for f in files:
        attached = RequestAttachment.query.filter_by(archived_file_id=f.id).first()
        if attached:
            continue

        FilePermission.query.filter_by(file_id=f.id).delete(synchronize_session=False)

        f.is_final_deleted = True
        f.final_deleted_at = datetime.utcnow()
        f.final_deleted_by = None

        db.session.add(AuditLog(
            action="ARCHIVE_FINAL_DELETE_RETENTION_JOB",
            user_id=None,
            target_type="ARCHIVE_FILE",
            target_id=f.id,
            note=f"File '{f.original_name}' moved to Super Trash by retention job"
        ))
        moved += 1

    db.session.commit()
    return moved
