from flask import Blueprint

from utils.timezone import format_local_datetime

workflow_bp = Blueprint(
    "workflow",
    __name__,
    url_prefix="/workflow"
)
workflow_bp.add_app_template_filter(format_local_datetime, "local_datetime")

# IMPORTANT: import routes after blueprint definition
from . import routes  # noqa: E402,F401
from . import templates_admin  # noqa: E402,F401
