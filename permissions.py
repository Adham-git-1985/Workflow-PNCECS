from functools import wraps
from flask_login import current_user
from flask import abort, request
import logging
import unicodedata


SUPER_ADMIN_ROLE = "SUPER_ADMIN"
logger = logging.getLogger(__name__)


def _normalize_role_text(value: str) -> str:
    text = (value or "").strip().upper().replace("-", "_").replace(" ", "_")
    try:
        text = unicodedata.normalize("NFKC", text)
        text = "".join(ch for ch in text if (ch.isalnum() or ch == "_"))
    except Exception:
        pass
    return text


def _role_looks_like_super_admin(raw_role: str) -> bool:
    norm = _normalize_role_text(raw_role)
    if norm in ("SUPERADMIN", "SUPER_ADMIN"):
        return True
    if ("SUPER" in norm and "ADMIN" in norm) or ("SYSTEM" in norm and "ADMIN" in norm):
        return True

    raw = (raw_role or "").strip()
    raw_lower = raw.lower()
    if "super" in raw_lower and "admin" in raw_lower:
        return True
    if "سوبر" in raw and ("ادمن" in raw or "أدمن" in raw):
        return True
    if "مدير" in raw and "نظام" in raw and any(word in raw for word in ("أعلى", "اعلى", "عليا", "الأعلى", "الاعلى")):
        return True
    return False


def _user_is_super_admin(user) -> bool:
    try:
        if getattr(user, "has_role", None) and (user.has_role("SUPER_ADMIN") or user.has_role("SUPERADMIN")):
            return True
    except Exception:
        pass

    try:
        if _role_looks_like_super_admin(getattr(user, "role", "") or ""):
            return True
    except Exception:
        pass

    try:
        if getattr(user, "id", None) == 1:
            return True
    except Exception:
        pass

    return False


def _user_has_required_role(user, role: str) -> bool:
    role = (role or "").strip()
    if not role:
        return False

    try:
        if getattr(user, "has_role", None) and user.has_role(role):
            return True
    except Exception:
        pass

    try:
        want = _normalize_role_text(role)
        mine = _normalize_role_text(getattr(user, "role", "") or "")
        if mine == want:
            return True
        if mine in ("SUPERADMIN", "SUPER_ADMIN") and want == "ADMIN":
            return True
    except Exception:
        pass

    return False


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

            # SUPER ADMIN bypass (supports legacy SUPERADMIN)
            if _user_is_super_admin(current_user):
                return f(*args, **kwargs)

            # Any allowed role
            for r in allowed_roles:
                if _user_has_required_role(current_user, r):
                    return f(*args, **kwargs)

            logger.warning(
                "Forbidden by roles_required | path=%s | user_id=%s | email=%s | role=%s | required=%s",
                getattr(request, "path", None),
                getattr(current_user, "id", None),
                getattr(current_user, "email", None),
                getattr(current_user, "role", None),
                allowed_roles,
            )
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
            if _user_is_super_admin(current_user) or _user_has_required_role(current_user, "ADMIN"):
                return f(*args, **kwargs)

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
                logger.warning(
                    "Forbidden by role_perm_required | path=%s | user_id=%s | email=%s | role=%s | permission=%s",
                    getattr(request, "path", None),
                    getattr(current_user, "id", None),
                    getattr(current_user, "email", None),
                    getattr(current_user, "role", None),
                    permission,
                )
                abort(403)

            return f(*args, **kwargs)

        return wrapper

    return decorator
