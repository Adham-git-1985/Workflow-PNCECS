"""Small, framework-independent helpers for portal meetings."""

from __future__ import annotations

from collections.abc import Iterable


def normalize_agenda_order(
    submitted_ids: Iterable[object],
    current_ids: Iterable[object],
) -> list[int]:
    """Validate and normalize a complete agenda ordering.

    The submitted order must contain every current agenda item exactly once.
    This prevents a stale or manipulated form from moving an item that belongs
    to another meeting or silently dropping newly added items.
    """

    try:
        submitted = [int(value) for value in submitted_ids]
        current = [int(value) for value in current_ids]
    except (TypeError, ValueError) as exc:
        raise ValueError("Agenda order contains an invalid item id.") from exc

    if len(current) != len(set(current)):
        raise ValueError("Current agenda contains duplicate item ids.")
    if len(submitted) != len(set(submitted)):
        raise ValueError("Agenda order contains duplicate item ids.")
    if len(submitted) != len(current) or set(submitted) != set(current):
        raise ValueError("Agenda order does not match the current meeting agenda.")

    return submitted
