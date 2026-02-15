from flask import Blueprint


store_bp = Blueprint(
    "store",
    __name__,
    url_prefix="/store",
)


# ⚠️ هذا السطر ضروري
from . import routes  # noqa: E402,F401
