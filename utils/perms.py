# utils/perms.py
from functools import wraps
from flask import abort
from flask_login import current_user
import unicodedata

# Delegation-aware effective user (if Delegation feature exists)
try:
    from utils.permissions import get_effective_user  # type: ignore
except Exception:  # pragma: no cover
    get_effective_user = None


def _is_portal_key(k: str) -> bool:
    try:
        ku = (k or "").strip().upper()
    except Exception:
        return False
    return ku.startswith(("PORTAL_", "HR_", "CORR_", "STORE_", "TRANSPORT_", "FOLLOWUPS_"))


def perm_required(*keys):
    """Permission decorator.

    - SUPERADMIN always allowed.
    - Workflow ADMIN allowed by default for non-portal keys.
    - Portal/HR/Correspondence/Store keys require explicit grant.
    - An active delegation extends the logged-in user's permissions; it never
      removes permissions already granted to that account.
    """
    def deco(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)

            # IMPORTANT: Delegation must NOT reduce the privileges of a SUPER/ADMIN account.
            # We always evaluate the real logged-in user first.
            base_user = current_user
            try:
                role_raw = (getattr(base_user, "role", "") or "").strip().upper().replace("-", "_").replace(" ", "_")
                role_raw = unicodedata.normalize("NFKC", role_raw)
                role_raw = "".join(ch for ch in role_raw if (ch.isalnum() or ch == "_"))
                if role_raw.startswith("SUPER"):
                    return f(*args, **kwargs)
                if role_raw == "ADMIN":
                    # ADMIN is allowed for non-portal keys below (same behavior as before)
                    pass
            except Exception:
                pass

            if hasattr(base_user, "has_role") and (base_user.has_role("SUPERADMIN") or base_user.has_role("SUPER_ADMIN")):
                return f(*args, **kwargs)

            # A delegatee may act with both their own permissions and the
            # delegator's permissions.  Keep both identities in the check so
            # enabling a delegation cannot unexpectedly lock the delegatee
            # out of pages they could already access.
            effective_user = base_user
            try:
                if callable(get_effective_user):
                    effective_user = get_effective_user() or base_user
            except Exception:
                effective_user = base_user

            candidates = [base_user]
            try:
                if (
                    effective_user is not None
                    and getattr(effective_user, "id", None) != getattr(base_user, "id", None)
                ):
                    candidates.append(effective_user)
            except Exception:
                candidates = [base_user]

            # Workflow ADMIN: allow only if all keys are NOT portal-like
            if keys and all(not _is_portal_key(k) for k in keys):
                for candidate in candidates:
                    try:
                        if candidate.has_role("ADMIN"):
                            return f(*args, **kwargs)
                    except Exception:
                        continue

            def _candidate_has(permission_key: str) -> bool:
                for candidate in candidates:
                    has_perm = getattr(candidate, "has_perm", None)
                    if not callable(has_perm):
                        continue
                    try:
                        if has_perm(permission_key):
                            return True
                    except Exception:
                        continue
                return False

            if not all(_candidate_has(k) for k in keys):
                abort(403)

            return f(*args, **kwargs)
        return wrapper
    return deco
