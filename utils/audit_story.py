"""Turn technical audit rows into simple Arabic story entries."""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

from utils.ui_labels import ui_label, ui_text


_ARABIC_WEEKDAYS = {
    0: "الاثنين",
    1: "الثلاثاء",
    2: "الأربعاء",
    3: "الخميس",
    4: "الجمعة",
    5: "السبت",
    6: "الأحد",
}

_FIELD_LABELS = {
    "file_id": "رقم الملف",
    "request_id": "رقم المعاملة",
    "step": "الخطوة",
    "step_order": "الخطوة",
    "source": "المصدر",
    "template": "المسار",
    "template_id": "رقم المسار",
    "user_id": "رقم المستخدم",
    "mentioned_user_id": "المستخدم المشار إليه",
    "role": "الدور",
    "status": "الحالة",
    "old_status": "الحالة السابقة",
    "new_status": "الحالة الجديدة",
    "filename": "اسم الملف",
    "file_name": "اسم الملف",
    "email": "البريد الإلكتروني",
    "reason": "السبب",
    "note": "الملاحظة",
}

_TARGET_LABELS = {
    "ARCHIVE_FILE": "ملف مؤرشف",
    "ARCHIVEDFILE": "ملف مؤرشف",
    "CORR_INBOUND": "مراسلة واردة",
    "CORR_OUTBOUND": "مراسلة صادرة",
    "MESSAGE": "رسالة",
    "ROLE": "دور مستخدم",
    "STORE_FILE": "ملف في المستودع",
    "USER": "مستخدم",
    "WORKFLOW_INSTANCE_STEP": "خطوة في المسار",
    "WORKFLOW_STEP": "خطوة في المسار",
    "WORKFLOW_STEP_TASK": "مهمة في المسار",
    "PARALLEL_TASK": "مهمة متوازية في المسار",
}

_REQUEST_TARGET_TYPES = {"WORKFLOWREQUEST", "WORKFLOW_REQUEST"}

_ACTION_TEMPLATES = {
    "PAGE_VIEW": "اطّلع {actor} على صفحة في النظام.",
    "USER_LOGIN": "سجّل {actor} الدخول إلى النظام.",
    "USER_LOGOUT": "سجّل {actor} الخروج من النظام.",
    "WORKFLOW_STARTED": "بدأ {actor} مسار المعاملة.",
    "WORKFLOW_COMPLETED": "أكمل {actor} مسار المعاملة.",
    "WORKFLOW_REPLY": "أضاف {actor} ردًا إلى المعاملة.",
    "WORKFLOW_ATTACHMENT_UPLOADED": "رفع {actor} مرفقًا للمعاملة.",
    "WORKFLOW_MENTION_ACCESS": "أضاف {actor} مستخدمًا إلى المعاملة عن طريق الإشارة إليه.",
    "DYNAMIC_BRANCH_SELECTED": "وجّه {actor} المسار الديناميكي إلى الفرع المختار.",
    "PARALLEL_SYNC_AUTHORIZED": "وجّه {actor} الخطوة المتزامنة إلى المعنيين المحددين.",
    "STEP_APPROVED": "وافق {actor} على الخطوة.",
    "STEP_REJECTED": "رفض {actor} الخطوة.",
    "REQUEST_DELETED": "حذف {actor} المعاملة.",
    "REQUEST_ESCALATION": "صعّد {actor} المعاملة للمتابعة.",
    "CORR_IN_CREATE": "أنشأ {actor} مراسلة واردة.",
    "CORR_OUT_CREATE": "أنشأ {actor} مراسلة صادرة.",
    "CORR_WORKFLOW_START": "بدأ {actor} مسار المراسلة.",
    "CORR_ATTACH_UPLOAD": "رفع {actor} مرفقًا للمراسلة.",
    "PORTAL_ACCESS_REQUEST_CREATE": "قدّم {actor} طلبًا للحصول على صلاحية.",
    "PORTAL_ACCESS_REQUEST_APPROVE": "وافق {actor} على طلب الصلاحية.",
    "PORTAL_ACCESS_REQUEST_REJECT": "رفض {actor} طلب الصلاحية.",
}

_ACTION_TOKEN_LABELS = {
    "ACCESS": "الصلاحية",
    "ACTIVE": "الحالة النشطة",
    "ADD": "إضافة",
    "ADMIN": "إداري",
    "APPROVE": "موافقة",
    "APPROVED": "موافقة",
    "ARCHIVE": "الأرشيف",
    "ATTACH": "مرفق",
    "ATTACHMENT": "مرفق",
    "AUDIT": "التدقيق",
    "AVATAR": "الصورة الشخصية",
    "BULK": "جماعي",
    "CANCEL": "إلغاء",
    "CHANGE": "تغيير",
    "COMPLETE": "إكمال",
    "COMPLETED": "إكمال",
    "CORR": "المراسلات",
    "CREATE": "إنشاء",
    "CUSTODY": "العهدة",
    "DELETE": "حذف",
    "DENIED": "مرفوض",
    "DOWNLOAD": "تنزيل",
    "EDIT": "تعديل",
    "EMAIL": "البريد الإلكتروني",
    "EMPLOYEE": "الموظف",
    "ESCALATION": "التصعيد",
    "FILE": "الملف",
    "FINAL": "نهائي",
    "HR": "الموارد البشرية",
    "IMPORT": "استيراد",
    "IN": "الوارد",
    "INBOUND": "الوارد",
    "INV": "المخزون",
    "ISSUE": "الصرف",
    "JOB": "مهمة آلية",
    "LOGIN": "الدخول",
    "LOGOUT": "الخروج",
    "MESSAGE": "الرسالة",
    "OUT": "الصادر",
    "OUTBOUND": "الصادر",
    "PAGE": "الصفحة",
    "PARALLEL": "متوازية",
    "PASSWORD": "كلمة المرور",
    "PAYSLIPS": "قسائم الرواتب",
    "PERMISSIONS": "الصلاحيات",
    "PORTAL": "البوابة",
    "PROFILE": "الملف الشخصي",
    "PURGE": "حذف نهائي",
    "REJECT": "رفض",
    "REQUEST": "الطلب",
    "RESTORE": "استعادة",
    "RETENTION": "سياسة الاحتفاظ",
    "RETURN": "الإرجاع",
    "ROLE": "الدور",
    "SCRAP": "الإتلاف",
    "SEND": "إرسال",
    "SENT": "إرسال",
    "SHARE": "المشاركة",
    "SIGNED": "التوقيع",
    "START": "بدء",
    "STEP": "الخطوة",
    "STOCKTAKE": "الجرد",
    "STORE": "المستودع",
    "SUPERTRASH": "سلة المحذوفات الإدارية",
    "SYNC": "المزامنة",
    "TASK": "المهمة",
    "UNSHARE": "إيقاف المشاركة",
    "UPDATE": "تحديث",
    "UPLOAD": "رفع",
    "USER": "المستخدم",
    "VIEW": "العرض",
    "WORKFLOW": "المسار",
}

_CONNECTORS = (
    "بعد ذلك",
    "ثم",
    "لاحقًا",
    "وبعدها",
)


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _friendly_value(value: Any) -> str:
    if value is None:
        return "غير محدد"
    if isinstance(value, bool):
        return "نعم" if value else "لا"
    if isinstance(value, (list, tuple, set)):
        return "، ".join(_friendly_value(item) for item in value)
    if isinstance(value, dict):
        return _describe_mapping(value)

    text = str(value).strip()
    if not text:
        return "غير محدد"
    if re.fullmatch(r"[A-Z][A-Z0-9_\-]*", text):
        return ui_label(text)
    return ui_text(text)


def _field_label(key: Any) -> str:
    normalized = str(key or "").strip().lower()
    if normalized in _FIELD_LABELS:
        return _FIELD_LABELS[normalized]
    return ui_label(normalized.replace("-", "_").upper()).replace("_", " ")


def _describe_mapping(data: dict[Any, Any]) -> str:
    return "، ".join(
        f"{_field_label(key)}: {_friendly_value(value)}"
        for key, value in data.items()
    )


def humanize_audit_note(note: Any) -> str:
    """Make common JSON and key=value audit notes readable without losing detail."""
    if note is None:
        return ""

    text = str(note).strip()
    if not text:
        return ""

    if text[:1] in {"{", "["}:
        try:
            parsed = json.loads(text)
            return _friendly_value(parsed)
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

    parts = [part.strip() for part in re.split(r"\s*\|\s*", text) if part.strip()]
    if any("=" in part for part in parts):
        readable_parts = []
        for part in parts:
            if "=" not in part:
                readable_parts.append(ui_text(part))
                continue
            key, value = part.split("=", 1)
            readable_parts.append(f"{_field_label(key)}: {_friendly_value(value)}")
        return "، ".join(readable_parts)

    return ui_text(text)


def _actor_name(log: Any) -> str:
    user = _get(log, "user")
    actor = _get(user, "full_name") or _get(user, "email") or "النظام تلقائيًا"

    behalf_user = _get(log, "on_behalf_of_user")
    behalf_name = _get(behalf_user, "full_name") or _get(behalf_user, "email")
    if behalf_name:
        return f"{actor}، نيابةً عن {behalf_name}"
    return str(actor)


def _action_sentence(action: Any, actor: str) -> str:
    key = str(action or "").strip().upper()
    template = _ACTION_TEMPLATES.get(key)
    if template:
        return template.format(actor=actor)

    action_label = _friendly_action_label(key)
    if "APPROV" in key:
        return f"وافق {actor} على «{action_label}»."
    if "REJECT" in key or "DENIED" in key:
        return f"رفض {actor} «{action_label}»."
    if "RESTORE" in key:
        return f"استعاد {actor} «{action_label}»."
    if "DELETE" in key or "PURGE" in key:
        return f"حذف {actor} «{action_label}»."
    if "UPLOAD" in key or "ATTACH" in key:
        return f"رفع {actor} ملفًا ضمن «{action_label}»."
    if "DOWNLOAD" in key:
        return f"نزّل {actor} ملفًا ضمن «{action_label}»."
    if "UNSHARE" in key:
        return f"أوقف {actor} مشاركة عنصر ضمن «{action_label}»."
    if "SHARE" in key:
        return f"شارك {actor} عنصرًا ضمن «{action_label}»."
    if "CREATE" in key or key.endswith("_ADD"):
        return f"أنشأ {actor} عنصرًا جديدًا ضمن «{action_label}»."
    if "UPDATE" in key or "EDIT" in key or "CHANGE" in key:
        return f"حدّث {actor} البيانات ضمن «{action_label}»."
    if "START" in key:
        return f"بدأ {actor} «{action_label}»."
    if "COMPLETE" in key or "FINISH" in key:
        return f"أكمل {actor} «{action_label}»."
    if "SEND" in key or "SENT" in key:
        return f"أرسل {actor} عنصرًا ضمن «{action_label}»."
    if "CANCEL" in key:
        return f"ألغى {actor} «{action_label}»."
    if "VIEW" in key or "OPEN" in key:
        return f"اطّلع {actor} على «{action_label}»."
    return f"نفّذ {actor} إجراء «{action_label}»."


def _friendly_action_label(action: str) -> str:
    if not action:
        return "إجراء في النظام"

    mapped = ui_label(action)
    if mapped and mapped != action:
        return mapped

    translated = [
        _ACTION_TOKEN_LABELS[token]
        for token in re.split(r"[_\-]+", action)
        if token in _ACTION_TOKEN_LABELS
    ]
    return " ".join(translated) if translated else "إجراء في النظام"


def _effective_request_id(log: Any) -> int | None:
    request_id = _get(log, "request_id")
    if request_id:
        try:
            return int(request_id)
        except (TypeError, ValueError):
            return None

    target_type = str(_get(log, "target_type") or "").strip().upper().replace(" ", "_")
    if target_type.replace("_", "") in _REQUEST_TARGET_TYPES:
        try:
            return int(_get(log, "target_id"))
        except (TypeError, ValueError):
            return None
    return None


def _target_summary(log: Any, request_id: int | None) -> str:
    if request_id:
        return ""
    target_id = _get(log, "target_id")
    target_type = str(_get(log, "target_type") or "").strip().upper().replace(" ", "_")
    if not target_type:
        return ""
    label = _TARGET_LABELS.get(target_type) or ui_label(target_type)
    if target_id is not None:
        return f"{label} رقم {target_id}"
    return str(label)


def _request_summary(request_id: int | None, request_meta: dict[int, dict[str, Any]]) -> str:
    if not request_id:
        return ""
    meta = request_meta.get(int(request_id), {}) or {}
    parts = [f"المعاملة رقم {request_id}"]
    if meta.get("request_type"):
        parts.append(str(meta["request_type"]))
    if meta.get("template_name"):
        parts.append(f"ضمن مسار {meta['template_name']}")
    return " — ".join(parts)


def _status_sentence(log: Any) -> str:
    old_status = _get(log, "old_status")
    new_status = _get(log, "new_status")
    if old_status and new_status and str(old_status) != str(new_status):
        return f"وانتقلت الحالة من «{ui_label(old_status)}» إلى «{ui_label(new_status)}»."
    if new_status:
        return f"وأصبحت الحالة «{ui_label(new_status)}»."
    return ""


def build_audit_story_entries(
    logs: Iterable[Any],
    request_meta: dict[int, dict[str, Any]] | None = None,
    log_steps: dict[int, int] | None = None,
) -> list[dict[str, Any]]:
    """Return oldest-to-newest story cards for the supplied audit page."""
    request_meta = request_meta or {}
    log_steps = log_steps or {}
    entries: list[dict[str, Any]] = []
    day_positions: dict[str, int] = {}

    def sort_key(log: Any) -> tuple[Any, int]:
        created_at = _get(log, "created_at")
        log_id = _get(log, "id", 0) or 0
        return created_at, int(log_id)

    for log in sorted(list(logs or []), key=sort_key):
        created_at = _get(log, "created_at")
        if created_at is None:
            continue

        day_key = created_at.strftime("%Y-%m-%d")
        position = day_positions.get(day_key, 0)
        day_positions[day_key] = position + 1
        connector = "في بداية اليوم" if position == 0 else _CONNECTORS[(position - 1) % len(_CONNECTORS)]

        request_id = _effective_request_id(log)
        log_id = int(_get(log, "id", 0) or 0)
        step = log_steps.get(log_id)
        actor = _actor_name(log)

        entries.append({
            "id": log_id,
            "day_key": day_key,
            "day_label": f"{_ARABIC_WEEKDAYS.get(created_at.weekday(), '')}، {created_at.strftime('%d/%m/%Y')}",
            "time_label": created_at.strftime("%H:%M"),
            "connector": connector,
            "sentence": _action_sentence(_get(log, "action"), actor),
            "detail": humanize_audit_note(_get(log, "note")),
            "status_sentence": _status_sentence(log),
            "request_id": request_id,
            "request_summary": _request_summary(request_id, request_meta),
            "target_summary": _target_summary(log, request_id),
            "step": int(step) if step is not None else None,
        })

    return entries
