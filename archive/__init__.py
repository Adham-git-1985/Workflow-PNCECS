from flask import Blueprint

archive_bp = Blueprint(
    "archive",
    __name__,
    url_prefix="/archive"
)

from . import routes
