from flask import Blueprint

workflow_bp = Blueprint(
    "workflow",
    __name__,
    url_prefix="/workflow"
)

from . import routes
