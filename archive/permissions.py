from sqlalchemy import or_
from extensions import db
from models import ArchivedFile, FilePermission
from datetime import datetime


# =========================
# Visibility constants
# =========================
VIS_PUBLIC = "PUBLIC"
VIS_DEPARTMENT = "DEPARTMENT"
VIS_PRIVATE = "PRIVATE"


# =========================
# View permission
# =========================
def can_view_archive_file(user, file):

    # Admin sees everything
    if user.has_role("ADMIN"):
        return True

    # Public file
    if file.visibility == VIS_PUBLIC:
        return True

    # Owner always sees his file
    if file.owner_id == user.id:
        return True

    # Department visibility
    if (
        file.visibility == VIS_DEPARTMENT
        and user.department_id == file.department_id
    ):
        return True

    # Shared permission (non-expired)
    perm = (
        FilePermission.query
        .filter(
            FilePermission.file_id == file.id,
            FilePermission.user_id == user.id,
            or_(
                FilePermission.expires_at == None,
                FilePermission.expires_at > datetime.utcnow()
            )
        )
        .first()
    )
    if perm:
        return True

    return False



def can_edit_archive_file(user, file):

    if user.has_role("ADMIN"):
        return True

    if file.owner_id == user.id:
        return True

    return False


def can_manage_archive_file(user, file):

    if user.has_role("ADMIN"):
        return True

    if (
        user.has_role("DEPT_HEAD")
        and user.department_id == file.department_id
    ):
        return True

    return False

def archive_access_query(user):
    """
    Centralized access control for archive files.
    Handles:
    - Admin: all files
    - User: own files + shared files
    - Department user: own + department + shared
    """

    # قاعدة أساسية: لا ملفات محذوفة
    base_query = ArchivedFile.query.filter(
        ArchivedFile.is_deleted == False
    )

    # Admin يشوف كل شيء
    if user.has_role("ADMIN"):
        return base_query

    # Subquery واحد فقط (مشترك + غير منتهي)
    shared_file_ids = (
        db.session.query(FilePermission.file_id)
        .filter(
            FilePermission.user_id == user.id,
            or_(
                FilePermission.expires_at == None,
                FilePermission.expires_at > datetime.utcnow()
            )
        )
        .subquery()
    )

    conditions = [
        ArchivedFile.owner_id == user.id,
        ArchivedFile.id.in_(shared_file_ids)
    ]

    # ملفات الدائرة فقط إذا عنده صلاحية دائرة
    # (عدّل الشرط حسب نظام الصلاحيات عندك)
    if user.department_id and user.has_role("DEPT"):
        conditions.append(
            ArchivedFile.department_id == user.department_id
        )

    return base_query.filter(
        or_(*conditions)
    )
