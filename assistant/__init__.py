from flask import Blueprint


assistant_bp = Blueprint(
    "assistant",
    __name__,
    url_prefix="/api/assistant",
)


from . import routes  # noqa: E402,F401
