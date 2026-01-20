from permissions.matrix import PERMISSION_MATRIX

def has_permission(user, permission):
    if not user or not user.role:
        return False
    return permission in PERMISSION_MATRIX.get(user.role, [])
