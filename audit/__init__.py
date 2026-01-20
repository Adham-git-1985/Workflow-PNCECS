from flask import Blueprint

audit_bp = Blueprint(
    "audit",
    __name__,
    url_prefix="/audit"
)

from . import routes
