from datetime import datetime

from sqlalchemy import or_
from extensions import db
from models import ArchivedFile, FilePermission, RequestAttachment, WorkflowRequest
from archive.permissions import (
    VIS_PUBLIC,
    VIS_DEPARTMENT,
)
from services.workflow_confidentiality import (
    can_user_pass_confidential_workflow_gate,
)


def _apply_confidential_workflow_guard(query, user):
    """Remove archive files linked to secret requests the user cannot access."""
    rows = (
        db.session.query(RequestAttachment.archived_file_id, WorkflowRequest)
        .join(WorkflowRequest, WorkflowRequest.id == RequestAttachment.request_id)
        .filter(WorkflowRequest.confidentiality == "SECRET")
        .all()
    )
    denied_file_ids = {
        int(file_id)
        for file_id, req in rows
        if file_id and not can_user_pass_confidential_workflow_gate(user, req)
    }
    if denied_file_ids:
        query = query.filter(~ArchivedFile.id.in_(denied_file_ids))
    return query


def archive_access_query(user):
    """
    Base query that returns only files the user is allowed to see.
    Rules:
    - Always excludes soft-deleted files.
    - Admin: all non-deleted files.
    - Others: own files + public + department (when applicable) + shared (non-expired).
    - Dept head: can see all dept files (even if visibility not explicitly set).
    """

    # Always exclude soft-deleted files
    base = ArchivedFile.query.filter(
        ArchivedFile.is_deleted.is_(False),
        (ArchivedFile.is_final_deleted.is_(False) | ArchivedFile.is_final_deleted.is_(None))
    )
    base = _apply_confidential_workflow_guard(base, user)

    # Admin sees everything (non-deleted)
    if user.has_role("ADMIN"):
        return base

    # Shared permissions (non-expired)
    # NOTE: Using a SELECT query directly (no .subquery()) to avoid IN(subquery) pitfalls/warnings
    shared_ids_select = (
        db.session.query(FilePermission.file_id)
        .filter(
            FilePermission.user_id == user.id,
            or_(
                FilePermission.expires_at.is_(None),
                FilePermission.expires_at > datetime.utcnow()
            )
        )
    )

    conditions = [
        # Own files
        ArchivedFile.owner_id == user.id,

        # Public visibility
        ArchivedFile.visibility == VIS_PUBLIC,

        # Shared (non-expired)
        ArchivedFile.id.in_(shared_ids_select),
    ]

    # Department visibility
    if user.department_id:
        # Visibility == department AND same department
        conditions.append(
            (ArchivedFile.visibility == VIS_DEPARTMENT) &
            (ArchivedFile.department_id == user.department_id)
        )

        # Dept Head can see all dept files (حتى لو الصلاحية غير محددة)
        if user.has_role("DEPT_HEAD"):
            conditions.append(
                ArchivedFile.department_id == user.department_id
            )

    return base.filter(or_(*conditions)).distinct()
