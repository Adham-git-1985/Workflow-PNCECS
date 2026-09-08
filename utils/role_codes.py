from __future__ import annotations

import re
import unicodedata


_GENERAL_SECRETARY_KEYS = {
    "GENERALSECRETARY",
    "SECRETARYGENERAL",
    "الأمينالعام",
    "الامينالعام",
}


def normalized_role_key(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().upper()
    return "".join(character for character in text if character.isalnum())


def canonical_role_key(value: str | None) -> str:
    key = normalized_role_key(value)
    if key in _GENERAL_SECRETARY_KEYS:
        return "GENERALSECRETARY"
    return key


def roles_equivalent(left: str | None, right: str | None) -> bool:
    left_key = canonical_role_key(left)
    right_key = canonical_role_key(right)
    return bool(left_key and left_key == right_key)


def role_storage_variants(value: str | None) -> set[str]:
    raw = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not raw:
        return set()

    parts = [part for part in re.split(r"[\s_-]+", raw) if part]
    variants = {
        raw,
        "_".join(parts),
        "-".join(parts),
        " ".join(parts),
        "".join(parts),
    }

    if canonical_role_key(raw) == "GENERALSECRETARY":
        variants.update({
            "General_secretary",
            "GENERAL_SECRETARY",
            "GENERAL-SECRETARY",
            "GENERAL SECRETARY",
            "GENERALSECRETARY",
            "SECRETARY_GENERAL",
            "SECRETARY-GENERAL",
            "SECRETARY GENERAL",
            "SECRETARYGENERAL",
            "الأمين العام",
            "الامين العام",
        })

    return {variant.strip().casefold() for variant in variants if variant.strip()}
