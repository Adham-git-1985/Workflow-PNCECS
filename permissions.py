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
