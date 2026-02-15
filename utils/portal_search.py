"""Simple, safe 'search across all columns' helper for portal list views."""

from __future__ import annotations

import re
from typing import Iterable, Optional

from sqlalchemy import String, and_, cast, or_
from sqlalchemy.sql import ColumnElement


def apply_search_all_columns(
    query,
    model,
    q: Optional[str],
    *,
    extra_columns: Optional[Iterable[ColumnElement]] = None,
    exclude_columns: Optional[Iterable[str]] = None,
):
    """Apply a free-text search across *all* columns of a SQLAlchemy model.

    - Splits the search text into terms (space-separated).
    - For each term, builds an OR across all model columns (and any extra_columns).
    - Combines terms with AND (so multiple words narrow the result).

    This is intentionally conservative and avoids relationship traversals.
    """

    if not q:
        return query

    q = (q or "").strip()
    if not q:
        return query

    terms = [t for t in re.split(r"\s+", q) if t]
    if not terms:
        return query

    exclude = set(exclude_columns or [])

    columns: list[ColumnElement] = []
    # Use casting to string so dates/ints become searchable too.
    for col in getattr(model, "__table__").columns:
        if col.name in exclude:
            continue
        try:
            columns.append(cast(getattr(model, col.name), String))
        except Exception:
            # Extremely rare: skip columns that can't be cast
            continue

    if extra_columns:
        for ec in extra_columns:
            columns.append(cast(ec, String))

    if not columns:
        return query

    term_filters = []
    for term in terms:
        pattern = f"%{term}%"
        ors = []
        for c in columns:
            try:
                ors.append(c.ilike(pattern))
            except Exception:
                continue
        if ors:
            term_filters.append(or_(*ors))

    if not term_filters:
        return query

    return query.filter(and_(*term_filters))
