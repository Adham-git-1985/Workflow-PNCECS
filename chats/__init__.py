from flask import Blueprint

chats_bp = Blueprint("chats", __name__, url_prefix="/chats")

from . import routes  # noqa: E402,F401
