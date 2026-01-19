from sqlalchemy import or_
from extensions import db
from models import ArchivedFile, FilePermission

def archive_access_query(user):
    if user.has_role("ADMIN"):
        return ArchivedFile.query.filter(
            ArchivedFile.is_deleted == False
        )

    return ArchivedFile.query.filter(
        or_(
            ArchivedFile.owner_id == user.id,
            ArchivedFile.department_id == user.department_id,
            ArchivedFile.id.in_(
                db.session.query(FilePermission.file_id)
                .filter(FilePermission.user_id == user.id)
            )
        ),
        ArchivedFile.is_deleted == False
    )
