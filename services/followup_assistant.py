"""Local, review-first suggestions for employee follow-up reports."""

from __future__ import annotations

import re
import unicodedata


def _normalise(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"[^\w\u0600-\u06ff]+", " ", text)
    return " ".join(text.split())


def build_followup_analysis(items) -> dict[str, object]:
    """Build deterministic local suggestions without sending report data away."""
    included_items = [item for item in (items or []) if getattr(item, "is_included", True)]
    completed = [item for item in included_items if (getattr(item, "status", "") or "").upper() == "COMPLETED"]
    incomplete = [item for item in included_items if (getattr(item, "status", "") or "").upper() != "COMPLETED"]

    title_groups: dict[str, list[object]] = {}
    for item in included_items:
        normalised = _normalise(getattr(item, "title", ""))
        if normalised:
            title_groups.setdefault(normalised, []).append(item)

    duplicate_ids: set[int] = set()
    duplicate_messages: list[str] = []
    for group in title_groups.values():
        if len(group) < 2:
            continue
        label = str(getattr(group[0], "title", "") or "بند مكرر").strip()
        duplicate_messages.append(f"قد يكون بند «{label}» مكرراً.")
        duplicate_ids.update(int(item.id) for item in group if getattr(item, "id", None))

    if completed:
        summary = f"تم إنجاز {len(completed)} بند خلال الفترة المحددة."
    elif included_items:
        summary = "تمت مراجعة بنود التقرير، ولا توجد بنود مكتملة محددة حالياً."
    else:
        summary = "لا توجد بنود مضافة بعد؛ أضف الإنجازات أو استخرجها من المهام المكتملة."

    if incomplete:
        summary += f" وهناك {len(incomplete)} بند يحتاج متابعة."

    suggestions: dict[int, str] = {}
    for item in included_items:
        title = str(getattr(item, "title", "") or "").strip()
        description = str(getattr(item, "description", "") or "").strip()
        status = (getattr(item, "status", "") or "").upper()
        if status == "COMPLETED":
            suggestion = f"تم إنجاز: {title}."
        else:
            suggestion = f"قيد المتابعة: {title}."
        if description:
            suggestion = f"{suggestion} {description}"
        if getattr(item, "id", None):
            suggestions[int(item.id)] = suggestion[:1500]

    notes = []
    if duplicate_messages:
        notes.extend(duplicate_messages)
    if incomplete:
        names = "، ".join(str(getattr(item, "title", "") or "").strip() for item in incomplete[:5])
        notes.append(f"بنود غير مكتملة تحتاج توضيحاً أو خطة متابعة: {names}.")
    if not notes:
        notes.append("لم يكتشف المساعد المحلي تكراراً أو بنوداً غير مكتملة.")

    return {
        "summary": summary,
        "notes": "\n".join(notes),
        "suggestions": suggestions,
        "duplicate_ids": duplicate_ids,
    }
