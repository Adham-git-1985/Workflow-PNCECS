from functools import wraps
from flask_login import current_user
from flask import abort
import unicodedata


SUPER_ADMIN_ROLE = "SUPER_ADMIN"


def roles_required(*roles):
    """Role gate that respects User.has_role() normalization.

    - SUPER_ADMIN / SUPERADMIN → always allowed.
    - Otherwise, user must match one of the required roles.

    Roles are compared using current_user.has_role(), which normalizes legacy names.
    """

    allowed_roles = [str(r).strip() for r in roles if r]

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)

                # Robust SUPER/ADMIN bypass even if has_role is missing or role is stored as a label
                try:
                    raw = (getattr(current_user, 'role', '') or '').strip()
                    norm = raw.upper().replace('-', '_').replace(' ', '_')
                    norm = unicodedata.normalize('NFKC', norm)
                    norm = ''.join(ch for ch in norm if (ch.isalnum() or ch == '_'))
                    if norm.startswith('SUPER') or ('SUPER' in norm and 'ADMIN' in norm):
                        return f(*args, **kwargs)
                except Exception:
                    pass
                # Ultimate safe fallback: first user (id=1) is treated as SUPER_ADMIN
                try:
                    if getattr(current_user, 'id', None) == 1:
                        return f(*args, **kwargs)
                except Exception:
                    pass


            # SUPER ADMIN bypass (supports legacy SUPERADMIN)
            try:
                if current_user.has_role("SUPER_ADMIN") or current_user.has_role("SUPERADMIN"):
                    return f(*args, **kwargs)
            except Exception:
                pass

            # Any allowed role
            for r in allowed_roles:
                try:
                    if current_user.has_role(r):
                        return f(*args, **kwargs)
                except Exception:
                    continue

            abort(403)

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