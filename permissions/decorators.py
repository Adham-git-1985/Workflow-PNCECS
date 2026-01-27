from functools import wraps
from flask import abort
from flask_login import current_user
from permissions.check import has_permission


def permission_required(permission):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not has_permission(current_user, permission):
                abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return decorator
