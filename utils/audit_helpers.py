from flask import g
from flask_login import current_user

def delegation_audit_fields() -> dict:
    """Returns extra AuditLog fields when user is acting via delegation."""
    try:
        d = getattr(g, "delegation", None)
        eff = getattr(g, "effective_user", None)
        if d and eff and getattr(current_user, "is_authenticated", False):
            if getattr(eff, "id", None) and eff.id != current_user.id:
                return {"on_behalf_of_id": eff.id, "delegation_id": d.id}
    except Exception:
        pass
    return {}
