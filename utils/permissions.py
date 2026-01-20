from functools import wraps
from flask import abort
from flask_login import current_user

# =========================
# Effective User (Delegation-ready)
# =========================
def get_effective_user():
    """
    المستخدم الفعلي (حاليًا بدون Delegation)
    جاهز للربط لاحقًا
    """
    return current_user


# =========================
# Admin only
# =========================
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            abort(401)

        if getattr(current_user, "role", None) != "ADMIN":
            abort(403)

        return f(*args, **kwargs)
    return decorated_function


# =========================
# Request access
# =========================
def can_access_request(request_obj, user):
    if request_obj.requester_id == user.id:
        return True

    if request_obj.current_role == user.role:
        return True

    if user.role == "ADMIN":
        return True

    return False


# =========================
# Permissions (RBAC)
# =========================
def has_permission(user, permission):
    if user.role == "ADMIN":
        return True

    from models import RolePermission
    from extensions import db

    return db.session.query(RolePermission).filter_by(
        role=user.role,
        permission=permission
    ).first() is not None


def permission_required(permission):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not has_permission(current_user, permission):
                abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return decorator
