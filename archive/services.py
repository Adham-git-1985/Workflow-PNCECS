# archive/services.py

from datetime import datetime

from sqlalchemy import func, distinct
from extensions import db
from models import ArchivedFile, FilePermission
from archive.permissions import VIS_PUBLIC, VIS_DEPARTMENT
from archive.queries import archive_access_query


def _active_permission_filter():
    """Active share permission: not expired."""
    return (
        (FilePermission.expires_at.is_(None)) |
        (FilePermission.expires_at > datetime.utcnow())
    )


def get_archive_counters(user):
    """
    Returns counters used in archive UI.
    Keys (safe defaults):
      - total: total files user can access (permissions-aware)
      - mine: files uploaded/owned by user
      - shared: count of distinct files shared with user (active shares)
      - department: files in user's department (depends on role/visibility rules)
      - public: public visible files (non-deleted)
    """

    # total accessible (permissions-aware)
    total = archive_access_query(user).count()

    # mine
    mine = (
        ArchivedFile.query
        .filter(
            ArchivedFile.is_deleted.is_(False),
            ArchivedFile.owner_id == user.id
        )
        .count()
    )

    # shared (distinct file_id) - exclude deleted by joining ArchivedFile
    shared = (
        db.session.query(func.count(distinct(FilePermission.file_id)))
        .join(ArchivedFile, ArchivedFile.id == FilePermission.file_id)
        .filter(
            ArchivedFile.is_deleted.is_(False),
            FilePermission.user_id == user.id,
            _active_permission_filter(),
        )
        .scalar()
    ) or 0

    # public
    public = (
        ArchivedFile.query
        .filter(
            ArchivedFile.is_deleted.is_(False),
            ArchivedFile.visibility == VIS_PUBLIC
        )
        .count()
    )

    # department
    department = 0
    if user.department_id:
        if user.has_role("DEPT_HEAD"):
            # dept head sees all dept files (non-deleted)
            department = (
                ArchivedFile.query
                .filter(
                    ArchivedFile.is_deleted.is_(False),
                    ArchivedFile.department_id == user.department_id
                )
                .count()
            )
        else:
            # normal users: only department visibility + same department
            department = (
                ArchivedFile.query
                .filter(
                    ArchivedFile.is_deleted.is_(False),
                    ArchivedFile.visibility == VIS_DEPARTMENT,
                    ArchivedFile.department_id == user.department_id
                )
                .count()
            )

    return {
        "total": total,
        "mine": mine,
        "shared": shared,
        "department": department,
        "public": public,
    }


def get_shared_count_map(files):
    """
    Returns dict: {file_id: active_share_count}
    Counts only non-delegated shares (delegated are handled separately in your route).
    """
    if not files:
        return {}

    ids = [f.id for f in files]

    rows = (
        db.session.query(FilePermission.file_id, func.count(FilePermission.id))
        .filter(
            FilePermission.file_id.in_(ids),
            FilePermission.delegated_by.is_(None),     # exclude delegated grants
            _active_permission_filter(),
        )
        .group_by(FilePermission.file_id)
        .all()
    )

    return {file_id: cnt for file_id, cnt in rows}
