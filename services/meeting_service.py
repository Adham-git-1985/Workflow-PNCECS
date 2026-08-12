"""Small, framework-independent helpers for portal meetings."""

from __future__ import annotations

from collections.abc import Iterable
from io import BytesIO
import zipfile


RECORDED_ATTENDANCE_LABELS = {
    "ATTENDED": "حضر",
    "ABSENT": "تغيب",
}


def recorded_attendance_label(status: object) -> str:
    """Return the binary attendance label used in official minutes."""

    normalized = str(status or "").strip().upper()
    return RECORDED_ATTENDANCE_LABELS.get(normalized, "تغيب")


def validate_docx_package(data: bytes) -> None:
    """Reject incomplete or corrupt DOCX packages before download."""

    if not data:
        raise ValueError("The DOCX package is empty.")

    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            names = set(archive.namelist())
            required = {"[Content_Types].xml", "_rels/.rels", "word/document.xml"}
            if not required.issubset(names):
                raise ValueError("The DOCX package is missing required parts.")
            if archive.testzip() is not None:
                raise ValueError("The DOCX package contains a corrupt entry.")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("The DOCX package is invalid.") from exc


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
