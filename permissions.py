from functools import wraps
from flask_login import current_user
from flask import abort


SUPER_ADMIN_ROLE = "SUPER_ADMIN"


def roles_required(*roles):
    """Role gate with SUPER_ADMIN override.

    - If user role is SUPER_ADMIN → always allowed.
    - Otherwise, user must match one of the required roles.

    Roles are compared case-insensitively.
    """

    allowed_roles = {str(r).strip().upper() for r in roles if r}

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)

            user_role = (getattr(current_user, "role", "") or "").strip().upper()

            # SUPER ADMIN bypass
            if user_role == SUPER_ADMIN_ROLE:
                return f(*args, **kwargs)

            if user_role not in allowed_roles:
                abort(403)

            return f(*args, **kwargs)

        return decorated_function

    return decorator

def role_perm_required(permission: str):
    """Allow access if:
    - user is ADMIN (SUPER_ADMIN inherits), OR
    - user's role has the given permission in RolePermission table.
    """
    from functools import wraps
    from flask import abort
    from flask_login import login_required, current_user
    from models import RolePermission
    from sqlalchemy import func

    permission = (permission or "").strip().upper()
    if not permission:
        raise ValueError("permission is required")

    def decorator(f):
        @wraps(f)
        @login_required
        def wrapper(*args, **kwargs):
            # ADMIN / SUPER_ADMIN always allowed here
            try:
                if current_user.has_role("ADMIN") or current_user.has_role("SUPER_ADMIN"):
                    return f(*args, **kwargs)
            except Exception:
                pass

            # Per-user override via UserPermission (optional)
            try:
                from models import UserPermission
                user_ok = (
                    UserPermission.query
                    .filter_by(user_id=current_user.id, key=permission, is_allowed=True)
                    .first()
                )
                if user_ok:
                    return f(*args, **kwargs)
            except Exception:
                pass

            role = (getattr(current_user, "role", "") or "").strip()
            if not role:
                abort(403)

            role_norm = role.strip().lower()

            ok = (
                RolePermission.query
                .filter(func.lower(RolePermission.role) == role_norm)
                .filter(RolePermission.permission == permission)
                .first()
            )

            if not ok:
                abort(403)

            return f(*args, **kwargs)

        return wrapper

    return decorator
