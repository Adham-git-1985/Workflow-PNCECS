"""Portal-friendly query helpers.

This module provides a custom Flask-SQLAlchemy query class that can apply
**server-side sorting** for portal tables using URL params:

  ?sort=<field>&dir=asc|desc

To keep risk low, the behavior is scoped to routes under /portal.
"""

from __future__ import annotations

from typing import Optional

from flask import has_request_context, request
from flask_sqlalchemy.query import Query


class PortalSortableQuery(Query):
    """A Query that optionally applies portal sorting based on request args."""

    # Map "friendly" / template-facing sort keys to real DB columns.
    # We keep it by model *name* to avoid importing models (avoid circular imports).
    _ALIASES_BY_MODELNAME = {
        # models.User has @property full_name (built from `name`).
        "User": {"full_name": "name"},
    }

    def _portal_apply_sort(self) -> "PortalSortableQuery":
        """Apply sorting if on /portal and request has sort params."""
        if not has_request_context():
            return self

        # Scope strictly to portal pages
        path = (request.path or "")
        if not path.startswith("/portal"):
            return self

        sort_key = (request.args.get("sort") or "").strip()
        if not sort_key:
            return self

        direction = (request.args.get("dir") or "asc").strip().lower()
        direction = "desc" if direction == "desc" else "asc"

        # Only support simple single-entity queries (the common case for tables).
        try:
            # SQLAlchemy internal helper (works for ORM Query)
            mapper = self._only_full_mapper_zero("portal_sort")  # type: ignore[attr-defined]
            model = mapper.class_
        except Exception:
            return self

        # Never sort by relationships (unsafe / unpredictable without explicit joins)
        try:
            if sort_key in getattr(model, "__mapper__").relationships.keys():
                return self
        except Exception:
            pass

        # Apply alias mapping (ex: full_name -> name)
        model_aliases = self._ALIASES_BY_MODELNAME.get(getattr(model, "__name__", ""), {})
        sort_key_db = model_aliases.get(sort_key, sort_key)

        # Only allow sorting by real model attributes that produce SQL expressions
        col = getattr(model, sort_key_db, None)
        if col is None or not hasattr(col, "asc") or not hasattr(col, "desc"):
            return self

        try:
            expr = col.desc() if direction == "desc" else col.asc()
            # Clear existing ordering so the user's choice wins.
            return self.order_by(None).order_by(expr)
        except Exception:
            return self

    # --- Common execution methods used by list pages ---
    def all(self):  # type: ignore[override]
        q = self._portal_apply_sort()
        return super(PortalSortableQuery, q).all()

    def paginate(self, *args, **kwargs):  # type: ignore[override]
        q = self._portal_apply_sort()
        return super(PortalSortableQuery, q).paginate(*args, **kwargs)

    def first(self):  # type: ignore[override]
        q = self._portal_apply_sort()
        return super(PortalSortableQuery, q).first()
