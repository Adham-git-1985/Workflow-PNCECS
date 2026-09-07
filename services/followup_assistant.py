"""Local, review-first suggestions for employee follow-up reports."""

from __future__ import annotations

import re
import unicodedata


_MAX_SUGGESTION_CHARS = 420
_MAX_SUMMARY_ITEM_CHARS = 120
_DETAIL_LABEL = re.compile(
    r"^(?:تفاصيل(?:\s+المعاملة|\s+المهمة|\s+البند)?|ملاحظة(?:\s+الإجراء)?|الوصف)\s*[:：\-–—]\s*",
    flags=re.IGNORECASE,
)
_WORKFLOW_HEADLINES = {
    "بدء معاملة": "تم بدء معاملة «{subject}»",
    "متابعة واعتماد خطوة": "تمت متابعة واعتماد خطوة في «{subject}»",
    "اتخاذ قرار في خطوة": "تم اتخاذ قرار بشأن خطوة في «{subject}»",
    "متابعة خطوة متزامنة": "تمت متابعة خطوة متزامنة في «{subject}»",
    "إكمال مسار معاملة": "تم إكمال مسار معاملة «{subject}»",
}
_FEMININE_ACTIONS = {
    "أرشفة",
    "إدارة",
    "إعادة",
    "استجابة",
    "دراسة",
    "صياغة",
    "متابعة",
    "مراجعة",
    "معالجة",
    "مشاركة",
}
_ACTION_WORDS = _FEMININE_ACTIONS | {
    "إعداد",
    "إرسال",
    "إصدار",
    "إكمال",
    "إنجاز",
    "إنشاء",
    "اعتماد",
    "استكمال",
    "استخراج",
    "تحليل",
    "تحسين",
    "تحديث",
    "تدقيق",
    "تسليم",
    "تصميم",
    "تنسيق",
    "تنفيذ",
    "تطوير",
    "توثيق",
    "حصر",
    "رفع",
    "فحص",
}


def _normalise(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"[^\w\u0600-\u06ff]+", " ", text)
    return " ".join(text.split())


def _clean_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([،؛:.!?؟])", r"\1", text)
    return text.strip(" \t-–—•،؛:")


def _shorten(value: str, limit: int) -> str:
    text = _clean_text(value)
    if len(text) <= limit:
        return text

    target = max(1, limit - 1)
    minimum_cut = max(1, int(limit * 0.6))
    cut = -1
    for separator in ("؛", ".", "؟", "!", "،", " "):
        candidate = text.rfind(separator, minimum_cut, target)
        if candidate > cut:
            cut = candidate
    if cut < minimum_cut:
        cut = target
    return text[:cut].rstrip(" \t-–—•،؛:.!?؟") + "…"


def _content_parts(item) -> list[str]:
    parts: list[str] = []
    normalised_parts: list[str] = []
    for value in (getattr(item, "title", ""), getattr(item, "description", "")):
        for raw_part in re.split(r"\n+|(?<=[.!?؟؛])\s+", str(value or "")):
            part = _clean_text(_DETAIL_LABEL.sub("", raw_part))
            normalised = _normalise(part)
            if not normalised:
                continue
            if any(
                normalised == existing
                or (len(normalised) >= 35 and normalised in existing)
                for existing in normalised_parts
            ):
                continue
            parts.append(part)
            normalised_parts.append(normalised)
    return parts


def _workflow_completed_headline(headline: str) -> str | None:
    for label, template in _WORKFLOW_HEADLINES.items():
        match = re.match(rf"^{re.escape(label)}\s*[:：\-–—]\s*(.+)$", headline)
        if match:
            subject = _clean_text(match.group(1))
            return template.format(subject=subject) if subject else None
    return None


def _strip_employee_voice(value: str) -> str:
    text = _clean_text(value)
    text = re.sub(r"^(?:قمت|قمنا)\s+ب(?:ـ)?", "", text)
    text = re.sub(r"^(?:أنجزت|أنجزنا)\s+", "", text)
    text = re.sub(r"^العمل\s+على\s+", "", text)
    return _clean_text(text)


def _completed_headline(headline: str) -> str:
    workflow_headline = _workflow_completed_headline(headline)
    if workflow_headline:
        return workflow_headline

    cleaned = _strip_employee_voice(headline)
    existing = re.match(r"^تم(ت)?\s+(.+)$", cleaned)
    if existing:
        cleaned = _clean_text(existing.group(2))
    first_word = cleaned.split(maxsplit=1)[0] if cleaned else ""
    if first_word in _ACTION_WORDS:
        prefix = "تمت" if first_word in _FEMININE_ACTIONS else "تم"
        return f"{prefix} {cleaned}"
    return f"تم إنجاز {cleaned}" if cleaned else "تم إنجاز المهمة"


def _rewrite_item(item) -> str:
    parts = _content_parts(item)
    headline = parts[0] if parts else "مهمة"
    status = (getattr(item, "status", "") or "").upper()
    if status == "COMPLETED":
        suggestion = _completed_headline(headline)
    elif status == "IN_PROGRESS":
        suggestion = f"قيد التنفيذ: {_strip_employee_voice(headline)}"
    else:
        suggestion = f"لم يكتمل العمل على {_strip_employee_voice(headline)}، ويحتاج إلى متابعة"

    headline_normalised = _normalise(headline)
    details = [
        part
        for part in parts[1:]
        if _normalise(part) not in headline_normalised
    ]
    if details:
        suggestion += f"؛ وشمل ذلك {'، '.join(details)}"
    suggestion = _shorten(suggestion, _MAX_SUGGESTION_CHARS)
    return suggestion if suggestion.endswith((".", "؟", "!", "…")) else f"{suggestion}."


def _summary_focus(suggestion: str) -> str:
    focus = re.sub(
        r"^(?:تمت?|قيد التنفيذ\s*[:：]|لم يكتمل العمل على)\s+",
        "",
        suggestion,
    )
    focus = re.sub(r"[.؟!…]+$", "", focus)
    return _shorten(focus, _MAX_SUMMARY_ITEM_CHARS)


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

    suggestions: dict[int, str] = {}
    for item in included_items:
        if getattr(item, "id", None):
            suggestions[int(item.id)] = _rewrite_item(item)

    summary_items = [
        _summary_focus(suggestions[int(item.id)])
        for item in included_items[:5]
        if getattr(item, "id", None) and int(item.id) in suggestions
    ]
    if summary_items:
        summary = f"شملت أبرز الأعمال خلال الفترة: {'؛ '.join(summary_items)}."
    elif included_items:
        summary = "تمت مراجعة بنود التقرير، ولا توجد صياغات مكتملة متاحة حالياً."
    else:
        summary = "لا توجد بنود مضافة بعد؛ أضف الإنجازات أو استخرجها من المهام المكتملة."

    if completed:
        summary += f" بلغ عدد البنود المنجزة {len(completed)}."
    if incomplete:
        summary += f" وهناك {len(incomplete)} بند يحتاج متابعة."

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
