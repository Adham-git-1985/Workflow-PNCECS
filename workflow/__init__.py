from flask import Blueprint

workflow_bp = Blueprint(
    "workflow",
    __name__,
    url_prefix="/workflow"
)

# IMPORTANT: import routes after blueprint definition
from . import routes  # noqa: E402,F401
from . import templates_admin  # noqa: E402,F401
