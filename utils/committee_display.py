"""Compact, consistent committee-member display data for workflow screens."""

from __future__ import annotations

from collections.abc import Iterable

from models import Committee, User


def _role_key(value: str | None) -> str:
    """Normalize role codes the same way workflow routing does."""
    key = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    while "__" in key:
        key = key.replace("__", "_")
    return key.strip("_")


def _person_name(user: User | None) -> str:
    if not user:
        return ""
    return (getattr(user, "full_name", "") or getattr(user, "email", "") or "").strip()


def _unique_names(values: Iterable[str]) -> list[str]:
    """Keep configured order while avoiding duplicate people in the UI."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = (value or "").strip()
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        result.append(name)
    return result


def build_committee_summaries(
    committee_ids: Iterable[int] | None = None,
    *,
    committees: Iterable[Committee] | None = None,
    member_preview_limit: int = 2,
) -> dict[int, dict]:
    """Return display-safe committee people summaries keyed by committee id.

    The result is deliberately compact: the chair is always named, while only
    the first few other members are rendered directly.  Full names remain in
    ``member_names`` for an accessible title/tooltip where needed.
    """
    if committees is None:
        ids = {
            int(committee_id)
            for committee_id in (committee_ids or [])
            if committee_id
        }
        if not ids:
            return {}
        committee_rows = Committee.query.filter(Committee.id.in_(ids)).all()
    else:
        committee_rows = [committee for committee in committees if getattr(committee, "id", None)]

    if not committee_rows:
        return {}

    role_keys = {
        _role_key(assignee.role)
        for committee in committee_rows
        for assignee in (committee.assignees or [])
        if bool(getattr(assignee, "is_active", False))
        and (getattr(assignee, "kind", "") or "").strip().upper() == "ROLE"
        and _role_key(getattr(assignee, "role", None))
    }
    users_by_role: dict[str, list[User]] = {key: [] for key in role_keys}
    if role_keys:
        for user in User.query.filter(User.role.isnot(None)).order_by(User.id.asc()).all():
            role_key = _role_key(getattr(user, "role", None))
            if role_key in users_by_role:
                users_by_role[role_key].append(user)

    preview_limit = max(0, int(member_preview_limit))
    summaries: dict[int, dict] = {}
    for committee in committee_rows:
        chairs: list[str] = []
        members: list[str] = []
        for assignee in (committee.assignees or []):
            if not bool(getattr(assignee, "is_active", False)):
                continue

            kind = (getattr(assignee, "kind", "") or "").strip().upper()
            if kind == "USER":
                names = [_person_name(getattr(assignee, "user", None))]
            elif kind == "ROLE":
                role = (getattr(assignee, "role", None) or "").strip()
                names = [_person_name(user) for user in users_by_role.get(_role_key(role), [])]
                if not names and role:
                    names = [f"دور: {role}"]
            else:
                names = []

            if (getattr(assignee, "member_role", "") or "").strip().upper() == "CHAIR":
                chairs.extend(names)
            else:
                members.extend(names)

        chair_names = _unique_names(chairs)
        # A chair may be configured again as a member through another role.
        chair_keys = {name.casefold() for name in chair_names}
        member_names = [
            name for name in _unique_names(members)
            if name.casefold() not in chair_keys
        ]
        previews = member_names[:preview_limit]
        summaries[int(committee.id)] = {
            "name": getattr(committee, "label", None) or getattr(committee, "name_ar", "") or "لجنة",
            "chair": "، ".join(chair_names) if chair_names else "غير محدد",
            "chair_names": chair_names,
            "member_names": member_names,
            "member_preview": previews,
            "member_more_count": max(0, len(member_names) - len(previews)),
            "member_count": len(member_names),
        }
    return summaries
