from functools import wraps
from datetime import datetime

from flask import abort, g
from flask_login import current_user


def is_admin_like(user) -> bool:
    role = (getattr(user, "role", "") or "").strip().upper()
    return role in {"ADMIN", "SUPER_ADMIN"}


# =========================
# Delegation: Effective User
# =========================
def _load_delegation_context():
    """Loads delegation context into flask.g (if any).

    - g.delegations: list of active Delegation rows (effective now)
    - g.delegation: the "primary" active delegation (latest expiry) for backward compatibility
    - g.effective_user: delegator User for g.delegation (or current_user if no delegation)
    """
    if not getattr(current_user, "is_authenticated", False):
        g.delegations = []
        g.delegation = None
        g.effective_user = current_user
        g.delegation_checked = True
        return

    if getattr(g, "delegation_checked", False):
        return

    g.delegation_checked = True
    g.delegations = []
    g.delegation = None
    g.effective_user = current_user

    try:
        from models import Delegation, User

        now = datetime.now()
        delegations = (
            Delegation.query
            .filter(
                Delegation.to_user_id == current_user.id,
                Delegation.is_active.is_(True),
                Delegation.starts_at <= now,
                Delegation.expires_at >= now,
            )
            .order_by(Delegation.expires_at.desc(), Delegation.id.desc())
            .all()
        )

        g.delegations = delegations or []

        if g.delegations:
            d = g.delegations[0]
            eff = User.query.get(d.from_user_id)
            g.delegation = d
            g.effective_user = eff or current_user

    except Exception:
        g.delegations = []
        g.delegation = None
        g.effective_user = current_user

def get_effective_user():
    """Returns the effective user (delegator if current user is a delegatee)."""
    _load_delegation_context()
    return getattr(g, "effective_user", current_user)



def get_active_delegation():
    """Returns the primary active delegation row for the current user (or None)."""
    _load_delegation_context()
    return getattr(g, "delegation", None)


def get_active_delegations():
    """Returns all active delegations (effective now) for the current user."""
    _load_delegation_context()
    return list(getattr(g, "delegations", []) or [])


# =========================

# Admin only
# =========================
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            abort(401)

        if not is_admin_like(current_user):
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

    if is_admin_like(user):
        return True

    return False


# =========================
# Permissions (RBAC)
# =========================
def has_permission(user, permission):
    if is_admin_like(user):
        return True

    from models import RolePermission
    from extensions import db

    return (
        db.session.query(RolePermission)
        .filter_by(role=user.role, permission=permission)
        .first()
        is not None
    )


def permission_required(permission):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not has_permission(current_user, permission):
                abort(403)
            return fn(*args, **kwargs)

        return wrapper

    return decorator
