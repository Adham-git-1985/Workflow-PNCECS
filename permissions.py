from functools import wraps
from flask_login import current_user
from flask import abort


def roles_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):

            # المستخدم غير مسجّل دخول
            if not current_user.is_authenticated:
                abort(401)

            # المستخدم مسجّل لكن ليس لديه الدور المطلوب
            if current_user.role not in roles:
                abort(403)

            return f(*args, **kwargs)

        return decorated_function
    return decorator
