from flask import Blueprint

from utils.timezone import format_local_datetime


messages_bp = Blueprint(
    "messages",
    __name__,
    url_prefix="/messages"
)
messages_bp.add_app_template_filter(format_local_datetime, "local_datetime")


from . import routes  # noqa
