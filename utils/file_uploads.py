"""Shared policy and helpers for generic file attachments.

Import and data-processing endpoints (for example Excel imports or ZIP backups)
may still enforce their own format requirements.  Generic attachments, however,
must not be restricted by an extension allow-list.
"""

from __future__ import annotations

import unicodedata


SAFE_INLINE_MIME_TYPES = frozenset({
    "application/pdf",
    "image/bmp",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/tiff",
    "image/webp",
    "text/csv",
    "text/plain",
    "text/tab-separated-values",
})


def clean_original_filename(filename: str | None) -> str:
    """Return a display-safe basename without changing the visible extension."""
    if filename is None:
        return ""

    name = str(filename).replace("\x00", "").strip()
    # Handle filenames supplied by browsers using either path separator.
    name = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if name in {"", ".", ".."}:
        return ""
    return name


def is_allowed_attachment(filename: str | None) -> bool:
    """Accept every file type as long as it has a usable filename."""
    return bool(clean_original_filename(filename))


def storage_extension(filename: str | None) -> str:
    """Return a filesystem-safe final extension, without the leading dot.

    The original filename is retained in the database.  If an unusual extension
    cannot be represented safely on disk, the random stored filename simply has
    no extension; the upload itself is still accepted.
    """
    name = clean_original_filename(filename)
    if not name or "." not in name:
        return ""

    extension = name.rsplit(".", 1)[1].strip().lower()
    if not extension:
        return ""

    cleaned = []
    for char in extension:
        if unicodedata.category(char).startswith("C"):
            continue
        if char.isalnum() or char in {"-", "_"}:
            cleaned.append(char)

    return "".join(cleaned).strip("-_")


def random_storage_name(identifier: str, filename: str | None) -> str:
    """Build a random storage name while preserving a safe extension when possible."""
    extension = storage_extension(filename)
    return f"{identifier}.{extension}" if extension else identifier


def is_safe_inline_mimetype(mimetype: str | None) -> bool:
    """Limit inline rendering to inert browser-supported formats.

    HTML, SVG, scripts, executables, email messages, and unknown formats remain
    downloadable but are never rendered in the application's security origin.
    """
    normalized = (mimetype or "").split(";", 1)[0].strip().lower()
    return normalized in SAFE_INLINE_MIME_TYPES
