from __future__ import annotations


_CORR_ARABIC_PREFIXES = {"IN": "وارد", "OUT": "صادر"}


def correspondence_reference_label(
    kind: str | None,
    ref_no: str | int | None,
    *,
    include_number_word: bool = False,
) -> str:
    """Add the correspondence type only when it is absent from the reference."""
    normalized_kind = (kind or "IN").strip().upper()
    if normalized_kind not in _CORR_ARABIC_PREFIXES:
        normalized_kind = "IN"
    prefix = _CORR_ARABIC_PREFIXES[normalized_kind]
    value = str(ref_no or "").strip()
    if not value:
        return prefix

    if value == prefix or value.startswith((f"{prefix}-", f"{prefix}/", f"{prefix} ")):
        return value

    separator = " رقم " if include_number_word else " "
    return f"{prefix}{separator}{value}"
