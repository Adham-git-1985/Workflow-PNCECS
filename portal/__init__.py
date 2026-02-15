from flask import Blueprint, request, url_for
from markupsafe import Markup, escape

portal_bp = Blueprint("portal", __name__, url_prefix="/portal")


@portal_bp.app_context_processor
def inject_portal_sort_helpers():
    """Expose sorting helpers to all portal templates.

    Usage (inside <th>):
        {{ portal_sort_link('الاسم', 'name_ar') }}
    """

    def _current_sort() -> str:
        return (request.args.get("sort") or "").strip()

    def _current_dir() -> str:
        d = (request.args.get("dir") or "asc").lower().strip()
        return "desc" if d == "desc" else "asc"

    def portal_sort_url(field: str) -> str:
        """Build URL toggling sort for the given field, preserving other args."""
        field = (field or "").strip()
        if not field:
            return request.url

        args = request.args.to_dict(flat=True)

        # Reset pagination when changing sort
        args.pop("page", None)

        cur_sort = _current_sort()
        cur_dir = _current_dir()

        if cur_sort == field:
            args["dir"] = "desc" if cur_dir == "asc" else "asc"
        else:
            args["dir"] = "asc"

        args["sort"] = field

        view_args = dict(request.view_args or {})
        return url_for(request.endpoint, **view_args, **args)

    def portal_sort_icon(field: str) -> Markup:
        """Return a small icon indicating current sort direction."""
        field = (field or "").strip()
        cur_sort = _current_sort()
        if cur_sort != field:
            return Markup('<span class="portal-sort-icon text-muted">⇅</span>')

        cur_dir = _current_dir()
        if cur_dir == "asc":
            return Markup('<span class="portal-sort-icon">↑</span>')
        return Markup('<span class="portal-sort-icon">↓</span>')

    def portal_sort_link(label: str, field: str) -> Markup:
        """Return an <a> tag for a sortable column header."""
        href = portal_sort_url(field)
        return Markup(
            f'<a href="{escape(href)}" class="portal-sort-link">'
            f'{escape(label)} {portal_sort_icon(field)}'
            f"</a>"
        )

    return {
        "portal_sort_url": portal_sort_url,
        "portal_sort_icon": portal_sort_icon,
        "portal_sort_link": portal_sort_link,
        "portal_current_sort": _current_sort,
        "portal_current_dir": _current_dir,
    }

from . import routes  # noqa

from . import transport  # noqa
