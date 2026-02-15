from permissions.matrix import PERMISSION_MATRIX


def has_permission(user, permission):
    if not user or not getattr(user, "role", None):
        return False

    role = str(user.role).strip()
    if role.upper() == "SUPER_ADMIN":
        return True

    # normalize role as-is for matrix lookup (matrix contains mixed-case keys)
    return permission in PERMISSION_MATRIX.get(role, [])
