from flask import Blueprint

delegation_bp = Blueprint("delegation", __name__, url_prefix="/delegation")

from . import routes  # noqa
