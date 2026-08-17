"""Audience resolution and access control for portal circulars."""

from __future__ import annotations

from sqlalchemy import or_, select

from extensions import db
from models import Department, PortalCircular, User


CIRCULAR_SCOPE_ALL = "ALL"
CIRCULAR_SCOPE_DIRECTORATE = "DIRECTORATE"
CIRCULAR_SCOPE_DEPARTMENT = "DEPARTMENT"
CIRCULAR_SCOPES = {
    CIRCULAR_SCOPE_ALL,
    CIRCULAR_SCOPE_DIRECTORATE,
    CIRCULAR_SCOPE_DEPARTMENT,
}


def normalize_circular_scope(value: str | None) -> str:
    """Normalize stored values; missing legacy values mean the full committee."""
    scope = (value or CIRCULAR_SCOPE_ALL).strip().upper()
    return scope if scope in CIRCULAR_SCOPES else ""


def _user_can_manage_circulars(user) -> bool:
    try:
        return bool(user and user.has_perm("PORTAL_CIRCULARS_MANAGE"))
    except Exception:
        return False


def user_directorate_ids(user) -> set[int]:
    """Return explicit and department-derived directorate memberships."""
    ids: set[int] = set()
    try:
        if getattr(user, "directorate_id", None):
            ids.add(int(user.directorate_id))
    except (TypeError, ValueError):
        pass

    try:
        department_id = int(user.department_id) if getattr(user, "department_id", None) else None
    except (TypeError, ValueError):
        department_id = None
    if department_id:
        directorate_id = db.session.scalar(
            select(Department.directorate_id).where(Department.id == department_id)
        )
        if directorate_id:
            ids.add(int(directorate_id))
    return ids


def circular_visibility_filter(user):
    """SQL condition selecting circulars visible to ``user``."""
    if _user_can_manage_circulars(user):
        return True

    conditions = [
        PortalCircular.target_scope.is_(None),  # compatibility before backfill
        PortalCircular.target_scope == "",
        PortalCircular.target_scope == CIRCULAR_SCOPE_ALL,
    ]

    try:
        department_id = int(user.department_id) if getattr(user, "department_id", None) else None
    except (TypeError, ValueError):
        department_id = None
    if department_id:
        conditions.append(
            (PortalCircular.target_scope == CIRCULAR_SCOPE_DEPARTMENT)
            & (PortalCircular.target_department_id == department_id)
        )

    directorate_ids = user_directorate_ids(user)
    if directorate_ids:
        conditions.append(
            (PortalCircular.target_scope == CIRCULAR_SCOPE_DIRECTORATE)
            & (PortalCircular.target_directorate_id.in_(directorate_ids))
        )

    return or_(*conditions)


def visible_circulars_query(query, user):
    return query.filter(circular_visibility_filter(user))


def can_user_view_circular(row: PortalCircular, user) -> bool:
    """Object-level guard used by circular detail routes."""
    if _user_can_manage_circulars(user):
        return True

    scope = normalize_circular_scope(getattr(row, "target_scope", None))
    if scope == CIRCULAR_SCOPE_ALL:
        return True
    if scope == CIRCULAR_SCOPE_DEPARTMENT:
        try:
            return int(row.target_department_id) == int(user.department_id)
        except (TypeError, ValueError):
            return False
    if scope == CIRCULAR_SCOPE_DIRECTORATE:
        try:
            return int(row.target_directorate_id) in user_directorate_ids(user)
        except (TypeError, ValueError):
            return False
    return False


def circular_recipient_user_ids(row: PortalCircular) -> list[int]:
    """Resolve the exact internal recipients for a circular audience."""
    scope = normalize_circular_scope(getattr(row, "target_scope", None))
    query = db.session.query(User.id)

    if scope == CIRCULAR_SCOPE_ALL:
        pass
    elif scope == CIRCULAR_SCOPE_DEPARTMENT:
        try:
            target_id = int(row.target_department_id)
        except (TypeError, ValueError):
            return []
        query = query.filter(User.department_id == target_id)
    elif scope == CIRCULAR_SCOPE_DIRECTORATE:
        try:
            target_id = int(row.target_directorate_id)
        except (TypeError, ValueError):
            return []
        department_ids = select(Department.id).where(Department.directorate_id == target_id)
        query = query.filter(
            or_(
                User.directorate_id == target_id,
                User.department_id.in_(department_ids),
            )
        )
    else:
        return []

    return sorted({int(user_id) for (user_id,) in query.all() if user_id})
