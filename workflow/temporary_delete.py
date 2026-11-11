"""Authorization rules for temporary and permanent workflow deletion."""

from datetime import datetime, timedelta


TEMPORARY_DELETE_PERMISSION = "WORKFLOW_TEMPORARY_DELETE"
PERMANENT_DELETE_PERMISSION = "WORKFLOW_PERMANENT_DELETE"
TEMPORARY_DELETE_WINDOW = timedelta(hours=1)


def is_super_admin(user) -> bool:
    """Use the application's canonical super-admin check when available."""
    try:
        from permissions import _user_is_super_admin

        return bool(_user_is_super_admin(user))
    except Exception:
        return False


def is_admin_or_super_admin(user) -> bool:
    """Whether ``user`` is a built-in administrator account."""
    if is_super_admin(user):
        return True

    try:
        if user and user.has_role("ADMIN"):
            return True
    except Exception:
        pass

    role = (getattr(user, "role", "") or "").strip().upper().replace("-", "_").replace(" ", "_")
    return role == "ADMIN"


def has_direct_temporary_delete_permission(user) -> bool:
    """Check the explicitly assigned per-user permission.

    The revoke right must not be inherited through an active delegation: it is
    a short-lived authority given to the person who created the item.
    """
    try:
        from models import UserPermission

        return (
            UserPermission.query
            .filter_by(
                user_id=int(user.id),
                key=TEMPORARY_DELETE_PERMISSION,
                is_allowed=True,
            )
            .first()
            is not None
        )
    except Exception:
        return False


def has_direct_permanent_delete_permission(user) -> bool:
    """Check the explicit per-user right to permanently delete workflows.

    This intentionally reads ``UserPermission`` directly instead of relying on
    ``User.has_perm`` so a role or delegated identity cannot accidentally turn
    this destructive, user-scoped permission into a broad grant.
    """
    try:
        from models import UserPermission

        return (
            UserPermission.query
            .filter_by(
                user_id=int(user.id),
                key=PERMANENT_DELETE_PERMISSION,
                is_allowed=True,
            )
            .first()
            is not None
        )
    except Exception:
        return False


def can_permanently_delete_workflow(user) -> bool:
    """Whether ``user`` has permanent workflow deletion authority."""
    return is_admin_or_super_admin(user) or has_direct_permanent_delete_permission(user)


def can_revoke_within_window(user, *, owner_id, created_at, now=None) -> bool:
    """Whether ``user`` may revoke their own item during its first hour."""
    if not user or not owner_id or not created_at:
        return False

    try:
        if int(getattr(user, "id", 0) or 0) != int(owner_id):
            return False
    except (TypeError, ValueError):
        return False

    if not has_direct_temporary_delete_permission(user):
        return False

    current_time = now or datetime.utcnow()
    try:
        return created_at <= current_time < created_at + TEMPORARY_DELETE_WINDOW
    except TypeError:
        # A malformed or timezone-incompatible legacy timestamp must never
        # grant deletion access.
        return False


def can_delete_workflow_template(user, template, *, now=None) -> bool:
    """Allow normal, permanent, or owner's temporary template deletion."""
    if can_permanently_delete_workflow(user):
        return True

    try:
        if user and user.has_perm("WORKFLOW_TEMPLATES_DELETE"):
            return True
    except Exception:
        pass

    return can_revoke_within_window(
        user,
        owner_id=getattr(template, "created_by_id", None),
        created_at=getattr(template, "created_at", None),
        now=now,
    )


def can_delete_workflow_request(user, workflow_request, *, now=None) -> bool:
    """Allow permanent deletion authority or the requester's temporary revoke."""
    if can_permanently_delete_workflow(user):
        return True

    return can_revoke_within_window(
        user,
        owner_id=getattr(workflow_request, "requester_id", None),
        created_at=getattr(workflow_request, "created_at", None),
        now=now,
    )
