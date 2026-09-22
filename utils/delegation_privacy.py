"""Presentation rules for delegated work and restricted role terminology.

The system keeps the real executor in the audit trail.  That information is
only shown to the executor, the principal, and system administrators; everyone
else sees the principal as the actor of a delegated operation.
"""

from __future__ import annotations

import re
from typing import Any


COMPETENT_AUTHORITY_LABEL = "الإدارة صاحبة الاختصاص"


def _viewer(viewer: Any = None) -> Any:
    if viewer is not None:
        return viewer
    try:
        from flask_login import current_user

        return current_user
    except Exception:
        return None


def _user_id(user: Any) -> int | None:
    try:
        value = getattr(user, "id", user)
        return int(value) if value is not None else None
    except Exception:
        return None


def is_privileged_administrator(user: Any = None) -> bool:
    """Whether *user* may see protected administration/delegation details."""
    user = _viewer(user)
    if user is None:
        return False
    try:
        if any(user.has_role(role) for role in ("ADMIN", "SUPER_ADMIN", "SUPERADMIN")):
            return True
    except Exception:
        pass

    try:
        role = str(getattr(user, "role", "") or "").strip().upper()
        role = role.replace("-", "_").replace(" ", "_")
        return role in {"ADMIN", "SUPER_ADMIN", "SUPERADMIN"}
    except Exception:
        return False


_SUPER_ADMIN_TEXT = re.compile(
    r"(?i)(?:"
    r"السوبر\s*أدمن|السوبر\s*ادمن|سوبر\s*أدمن|سوبر\s*ادمن|"
    r"مدير\s+النظام\s+العام|"
    r"\bsuper[\s_-]*admin(?:istrator)?\b"
    r")",
)


def redact_super_admin_references(value: Any, viewer: Any = None) -> str:
    """Hide the super-admin title from viewers who are not administrators."""
    text = "" if value is None else str(value)
    if not text or is_privileged_administrator(viewer):
        return text
    return _SUPER_ADMIN_TEXT.sub(COMPETENT_AUTHORITY_LABEL, text)


def audit_principal(log: Any) -> Any:
    """Return the identity on whose behalf an audit operation was performed."""
    if not log:
        return None
    return getattr(log, "acting_for_user", None) or getattr(log, "on_behalf_of_user", None)


def audit_actual_actor(log: Any) -> Any:
    """Return the account that technically performed the operation."""
    if not log:
        return None
    return getattr(log, "actual_user", None) or getattr(log, "user", None)


def _audit_principal_id(log: Any) -> int | None:
    principal = audit_principal(log)
    return _user_id(principal) or _user_id(getattr(log, "acting_for_user_id", None)) or _user_id(
        getattr(log, "on_behalf_of_id", None)
    )


def _audit_actual_id(log: Any) -> int | None:
    actual = audit_actual_actor(log)
    return _user_id(actual) or _user_id(getattr(log, "actual_user_id", None)) or _user_id(
        getattr(log, "user_id", None)
    )


def is_delegated_audit(log: Any) -> bool:
    actual_id = _audit_actual_id(log)
    principal_id = _audit_principal_id(log)
    return bool(actual_id and principal_id and actual_id != principal_id)


def can_view_delegation_details(log: Any, viewer: Any = None) -> bool:
    """Return whether a viewer may know who actually executed an operation."""
    if not is_delegated_audit(log):
        return False
    viewer = _viewer(viewer)
    if is_privileged_administrator(viewer):
        return True
    viewer_id = _user_id(viewer)
    return viewer_id in {_audit_actual_id(log), _audit_principal_id(log)}


def audit_display_actor(log: Any, viewer: Any = None) -> Any:
    """Return the actor identity safe to display to *viewer*."""
    if is_delegated_audit(log) and not can_view_delegation_details(log, viewer):
        return audit_principal(log) or audit_actual_actor(log)
    return audit_actual_actor(log) or audit_principal(log)


def audit_display_actor_id(log: Any, viewer: Any = None) -> int | None:
    """ID counterpart of :func:`audit_display_actor` for compact projections."""
    if is_delegated_audit(log) and not can_view_delegation_details(log, viewer):
        return _audit_principal_id(log) or _audit_actual_id(log)
    return _audit_actual_id(log) or _audit_principal_id(log)


def audit_display_note(log: Any, viewer: Any = None) -> str:
    """Return an audit note without leaking the real executor to outsiders."""
    note = str(getattr(log, "note", "") or "")
    if not note or not is_delegated_audit(log) or can_view_delegation_details(log, viewer):
        return note

    actual = audit_actual_actor(log)
    principal = audit_principal(log)
    if not actual or not principal:
        return note

    principal_label = (
        getattr(principal, "full_name", None)
        or getattr(principal, "name", None)
        or getattr(principal, "username", None)
        or getattr(principal, "email", None)
        or "المستخدم المعني"
    )
    for attribute in ("full_name", "name", "username", "email"):
        value = str(getattr(actual, attribute, "") or "").strip()
        if value:
            note = re.sub(re.escape(value), principal_label, note, flags=re.IGNORECASE)

    actual_id = _audit_actual_id(log)
    if actual_id:
        # Older engine notes often carried a technical ``User#id`` instead of
        # a display name.  Hide that identifier when it denotes the delegate.
        note = re.sub(
            rf"\b(?:user|مستخدم)\s*#?\s*{re.escape(str(actual_id))}\b",
            principal_label,
            note,
            flags=re.IGNORECASE,
        )

    # Some legacy operation notes constructed a phrase such as
    # ``Delegate (مفوّض عن Principal)``.  Replacing the name alone would
    # still disclose that a delegation occurred, so strip that metadata from
    # the public presentation while retaining it for the authorized viewers.
    note = re.sub(
        r"\s*\((?:مفوّض|مفوض)\s+عن[^)]*\)",
        "",
        note,
        flags=re.IGNORECASE,
    )
    note = re.sub(
        r"\s*(?:بالنيابة|بموجب\s+تفويض)\s+عن\s+[^|\r\n]+",
        "",
        note,
        flags=re.IGNORECASE,
    )
    return note
