# utils/perms.py
from functools import wraps
from flask import abort
from flask_login import current_user

def perm_required(*keys):
    def deco(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)

            # ADMIN always allowed
            if hasattr(current_user, "has_role") and current_user.has_role("ADMIN"):
                return f(*args, **kwargs)

            has_perm = getattr(current_user, "has_perm", None)
            if not callable(has_perm):
                abort(403)

            if not all(has_perm(k) for k in keys):
                abort(403)

            return f(*args, **kwargs)
        return wrapper
    return deco
