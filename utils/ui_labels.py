import re


UI_LABELS_AR = {
    "DEPT": "دائرة",
    "DEPARTMENT": "دائرة",
    "DIRECTORATE": "إدارة",
    "WORKFLOW_STARTED": "بدء المسار",
    "STEP_APPROVED": "تمت الموافقة على الخطوة",
    "PENDING": "قيد الانتظار",
    "APPROVED": "موافق عليه",
    "REJECTED": "مرفوض",
    "IN_PROGRESS": "قيد التنفيذ",
    "BYPASSED": "تم التجاوز",
    "STEP": "خطوة",
    "DRAFT": "مسودة",
    "CANCELLED": "ملغي",
    "SUBMITTED": "مرسل",
}


def ui_label(value):
    if value is None:
        return ""
    text = str(value).strip()
    key = text.upper().replace(" ", "_")
    return UI_LABELS_AR.get(key, text)


def ui_text(value):
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"\bSTEP\s+(\d+)\b", r"الخطوة \1", text, flags=re.IGNORECASE)
    for key, label in sorted(UI_LABELS_AR.items(), key=lambda item: len(item[0]), reverse=True):
        text = re.sub(rf"\b{re.escape(key)}\b", label, text, flags=re.IGNORECASE)
    return text
