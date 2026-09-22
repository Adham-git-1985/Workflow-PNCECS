from functools import wraps
from datetime import datetime

from flask import abort, g, has_request_context, session
from flask_login import current_user
from utils.role_codes import roles_equivalent


# A legacy ``Delegation`` grants broad account-level acting.  It is retained
# for compatibility, but it must now be selected explicitly just like the
# newer scoped acting permissions and formal delegations.
LEGACY_DELEGATION_SESSION_KEY = "legacy_delegation_id"
IDENTITY_CHOICE_SESSION_KEY = "delegation_identity_choice_confirmed"


def is_admin_like(user) -> bool:
    role = (getattr(user, "role", "") or "").strip().upper()
    return role in {"ADMIN", "SUPER_ADMIN", "SUPERADMIN"}


def is_delegated_identity_selected() -> bool:
    """Whether this request is deliberately operating as another principal.

    A grant being available is not enough: the delegate must first choose an
    identity.  Once selected, authorization must use that single identity
    rather than merging the principal's permissions with the login account.
    """
    if not has_request_context() or not getattr(current_user, "is_authenticated", False):
        return False
    try:
        _load_delegation_context()
        effective = getattr(g, "effective_user", None)
        actual_id = int(current_user.id)
        effective_id = int(getattr(effective, "id", 0) or 0)
        if not effective_id or effective_id == actual_id:
            return False

        if getattr(g, "delegation", None) is not None:
            return True

        from utils.acting_authorization import get_execution_context

        return get_execution_context().get("execution_context", "SELF") != "SELF"
    except Exception:
        return False


def get_authorization_user():
    """Return the one identity whose roles and permissions apply now."""
    if is_delegated_identity_selected():
        try:
            return get_effective_user() or current_user
        except Exception:
            pass
    return current_user


# =========================
# Delegation: Effective User
# =========================
def _load_delegation_context():
    """Loads delegation context into flask.g (if any).

    - g.delegations: list of active Delegation rows (effective now)
    - g.delegation: the legacy delegation explicitly selected for this session
    - g.effective_user: selected principal, or the logged-in user in personal mode
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
        from models import Delegation
        from utils.acting_authorization import get_execution_context

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

        selected_id = _session_int(LEGACY_DELEGATION_SESSION_KEY)
        selected = next(
            (row for row in g.delegations if int(row.id) == int(selected_id or 0)),
            None,
        )
        g.delegation = selected

        # The newer scoped context has priority whenever it is selected.  It
        # is validated by its own service; the legacy context cannot silently
        # override it.
        execution = get_execution_context()
        principal = execution.get("acting_for_user")
        if execution.get("execution_context") != "SELF" and principal:
            g.effective_user = principal
        elif selected and selected.from_user:
            g.effective_user = selected.from_user

    except Exception:
        g.delegations = []
        g.delegation = None
        g.effective_user = current_user

def _session_int(key: str):
    if not has_request_context():
        return None
    try:
        value = session.get(key)
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def get_effective_user(user=None):
    """Return the identity explicitly selected for the logged-in account.

    Supplying another user is supported for old workflow helper call sites;
    those callers must keep that concrete user rather than accidentally adopt
    the current browser session's selection.
    """
    if user is not None:
        try:
            if not getattr(current_user, "is_authenticated", False) or int(user.id) != int(current_user.id):
                return user
        except (AttributeError, TypeError, ValueError):
            return user
    _load_delegation_context()
    return getattr(g, "effective_user", current_user)



def get_active_delegation():
    """Return the selected active legacy delegation, if any."""
    _load_delegation_context()
    return getattr(g, "delegation", None)


def get_active_delegations():
    """Return only the selected active delegation for the current identity.

    The old implementation exposed every active delegator at once.  That
    bypassed the user's required choice between their own identity and a
    principal's identity.
    """
    _load_delegation_context()
    selected = getattr(g, "delegation", None)
    return [selected] if selected else []


def get_available_delegations():
    """Return all currently usable legacy delegations for the account."""
    _load_delegation_context()
    return list(getattr(g, "delegations", []) or [])


def clear_legacy_delegation_selection() -> None:
    """Leave legacy delegation mode without affecting personal login identity."""
    if has_request_context():
        session.pop(LEGACY_DELEGATION_SESSION_KEY, None)
        for key in ("delegation_checked", "delegation", "effective_user"):
            g.pop(key, None)


def select_legacy_delegation(delegation_id):
    """Select one active legacy delegation for the current session."""
    if not getattr(current_user, "is_authenticated", False):
        raise PermissionError("يجب تسجيل الدخول أولاً")
    try:
        requested_id = int(delegation_id)
    except (TypeError, ValueError):
        raise PermissionError("التفويض المحدد غير صالح")

    row = next((item for item in get_available_delegations() if int(item.id) == requested_id), None)
    if not row:
        raise PermissionError("التفويض المحدد غير متاح أو انتهت مدته")

    # Scoped and legacy modes are mutually exclusive.  A requester must
    # consciously choose one principal at a time.
    try:
        from utils.acting_authorization import clear_execution_context

        clear_execution_context()
    except Exception:
        pass
    session[LEGACY_DELEGATION_SESSION_KEY] = int(row.id)
    mark_identity_choice_selected()
    for key in ("delegation_checked", "delegation", "effective_user"):
        g.pop(key, None)
    _load_delegation_context()
    return row


def mark_identity_choice_selected() -> None:
    if has_request_context():
        session[IDENTITY_CHOICE_SESSION_KEY] = True


def reset_identity_choice() -> None:
    """Start a fresh identity choice after a successful login."""
    if not has_request_context():
        return
    session.pop(IDENTITY_CHOICE_SESSION_KEY, None)
    clear_legacy_delegation_selection()
    for key in ("acting_permission_id", "acting_for_user_id", "formal_delegation_id"):
        session.pop(key, None)
    for key in (
        "execution_context_loaded",
        "actual_user",
        "actual_user_id",
        "acting_permission",
        "acting_permission_id",
        "formal_delegation",
        "formal_delegation_id",
        "acting_for_user",
        "acting_for_user_id",
        "execution_context",
    ):
        g.pop(key, None)


def prepare_identity_choice() -> bool:
    """Expose whether the logged-in delegate must choose a working identity."""
    if not getattr(current_user, "is_authenticated", False):
        g.delegation_identity_choice_pending = False
        return False

    _load_delegation_context()
    try:
        from utils.acting_authorization import get_active_acting_permissions, get_active_formal_delegations

        acting = get_active_acting_permissions(current_user.id)
        formal = get_active_formal_delegations(current_user.id)
    except Exception:
        acting, formal = [], []

    g.available_legacy_delegations = get_available_delegations()
    g.identity_acting_permissions = acting
    g.identity_formal_delegations = formal
    available = bool(g.available_legacy_delegations or acting or formal)
    pending = available and not bool(session.get(IDENTITY_CHOICE_SESSION_KEY))
    g.delegation_identity_choice_pending = pending
    return pending


# =========================

# Admin only
# =========================
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            abort(401)

        if not is_admin_like(get_authorization_user()):
            abort(403)

        return f(*args, **kwargs)

    return decorated_function


# =========================
# Request access
# =========================
def can_access_request(request_obj, user):
    if request_obj.requester_id == user.id:
        return True

    if roles_equivalent(request_obj.current_role, user.role):
        return True

    if is_admin_like(user):
        return True

    return False


# =========================
# Permissions (RBAC)
# =========================
def has_permission(user, permission):
    subject = user
    try:
        if (
            getattr(current_user, "is_authenticated", False)
            and int(getattr(user, "id", 0) or 0) == int(current_user.id)
            and is_delegated_identity_selected()
        ):
            subject = get_authorization_user()
    except Exception:
        subject = user

    if is_admin_like(subject):
        return True
    try:
        return bool(subject and subject.has_perm(permission))
    except Exception:
        return False


def permission_required(permission):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not has_permission(current_user, permission):
                abort(403)
            return fn(*args, **kwargs)

        return wrapper

    return decorator
