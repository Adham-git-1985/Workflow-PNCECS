# utils/perms.py
from functools import wraps
from flask import abort
from flask_login import current_user
import unicodedata

# Delegation-aware effective user (if Delegation feature exists)
try:
    from utils.permissions import (  # type: ignore
        get_effective_user,
        is_delegated_identity_selected,
    )
except Exception:  # pragma: no cover
    get_effective_user = None
    is_delegated_identity_selected = None


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
    - Once a delegated identity is selected, only the principal's permissions
      apply.  The technical login identity remains available only for audit.
    """
    def deco(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)

            base_user = current_user
            delegated_identity = False
            try:
                delegated_identity = bool(
                    callable(is_delegated_identity_selected)
                    and is_delegated_identity_selected()
                )
            except Exception:
                delegated_identity = False

            # In personal mode a super-admin keeps the usual bypass.  In a
            # selected delegated mode the principal is the permission subject.
            try:
                role_raw = (getattr(base_user, "role", "") or "").strip().upper().replace("-", "_").replace(" ", "_")
                role_raw = unicodedata.normalize("NFKC", role_raw)
                role_raw = "".join(ch for ch in role_raw if (ch.isalnum() or ch == "_"))
                if not delegated_identity and role_raw.startswith("SUPER"):
                    return f(*args, **kwargs)
                if role_raw == "ADMIN":
                    # ADMIN is allowed for non-portal keys below (same behavior as before)
                    pass
            except Exception:
                pass

            if (
                not delegated_identity
                and hasattr(base_user, "has_role")
                and (base_user.has_role("SUPERADMIN") or base_user.has_role("SUPER_ADMIN"))
            ):
                return f(*args, **kwargs)

            effective_user = base_user
            try:
                if callable(get_effective_user):
                    effective_user = get_effective_user() or base_user
            except Exception:
                effective_user = base_user

            candidates = [effective_user] if delegated_identity else [base_user]
            if not delegated_identity:
                try:
                    if (
                        effective_user is not None
                        and getattr(effective_user, "id", None) != getattr(base_user, "id", None)
                    ):
                        candidates.append(effective_user)
                except Exception:
                    candidates = [base_user]

            # A selected scoped acting/formal context can open the workflow
            # dashboard for the principal's work even when the real actor does
            # not have the principal's global dashboard permission.  The
            # request rows are still filtered by the context's VIEW scope in
            # the route itself; this only prevents the page-level decorator
            # from blocking that route before it can apply the row filter.
            if tuple(keys) == ("WORKFLOW_DASHBOARD_READ",):
                try:
                    from utils.acting_authorization import (
                        can_execute_action,
                        get_execution_context,
                    )

                    if (
                        get_execution_context().get("execution_context") != "SELF"
                        and can_execute_action(
                            "VIEW",
                            module_id="WORKFLOW",
                            require_formal=False,
                        )
                    ):
                        return f(*args, **kwargs)
                except Exception:
                    pass

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
