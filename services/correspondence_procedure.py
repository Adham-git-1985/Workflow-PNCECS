"""Procedural rules for inbound and outbound correspondence."""

from __future__ import annotations

from datetime import date, datetime


STATUS_LABELS = {
    "DRAFT": "مسودة",
    "RECEIVED": "مستلم",
    "IN_PROGRESS": "قيد الإجراء",
    "FORWARDED": "محول",
    "WAITING_INFO": "بانتظار استكمال",
    "WAITING_APPROVAL": "بانتظار الاعتماد",
    "RETURNED": "معاد",
    "APPROVED": "معتمد",
    "COMPLETED": "مكتمل",
    "CLOSED": "مغلق",
    "ARCHIVED": "مؤرشف",
}

ACTION_LABELS = {
    "REGISTER": "تسجيل المعاملة",
    "EDIT": "تحديث بيانات المعاملة",
    "OPEN": "فتح وبدء الإجراء",
    "REPLY": "الرد على البريد",
    "FORWARD": "تحويل البريد",
    "RETURN": "إعادة البريد إلى مرسله",
    "INTERNAL_NOTE": "إضافة ملاحظة داخلية",
    "REQUEST_INFO": "طلب استكمال معلومات أو مرفقات",
    "SAVE_DRAFT": "حفظ كمسودة",
    "SUBMIT_APPROVAL": "إرسال للاعتماد",
    "APPROVE": "اعتماد وإنهاء الإجراء",
    "FINAL_REPLY": "إصدار الرد الرسمي عبر مكتب الأمين العام",
    "CLOSE": "إغلاق المعاملة",
    "ARCHIVE": "أرشفة المعاملة",
    "DEADLINE_REMINDER": "تنبيه موعد نهائي",
    "WORKFLOW_SYNC": "تحديث تلقائي من مسار",
    "WORKFLOW_APPROVED": "اعتماد المعاملة في مسار",
    "WORKFLOW_REJECTED": "إعادة المعاملة من مسار",
}

QUEUE_LABELS = {
    "incoming": "البريد الوارد",
    "outgoing": "البريد الصادر",
    "internal": "البريد الداخلي",
    "to_me": "البريد المحول إليّ",
    "from_me": "البريد المحول مني",
    "waiting_action": "بانتظار الإجراء",
    "waiting_approval": "بانتظار الاعتماد",
    "completed": "البريد المكتمل",
    "archived": "البريد المؤرشف",
    "returned": "البريد المعاد",
    "high_priority": "عالي الأولوية",
    "confidential": "البريد السري",
}

ACTION_TRANSITIONS = {
    "OPEN": "IN_PROGRESS",
    "REPLY": "IN_PROGRESS",
    "FORWARD": "FORWARDED",
    "RETURN": "RETURNED",
    "REQUEST_INFO": "WAITING_INFO",
    "SAVE_DRAFT": "DRAFT",
    "SUBMIT_APPROVAL": "WAITING_APPROVAL",
    "APPROVE": "APPROVED",
    "FINAL_REPLY": "COMPLETED",
    "CLOSE": "CLOSED",
    "ARCHIVE": "ARCHIVED",
}

NOTE_REQUIRED_ACTIONS = {
    "REPLY",
    "FORWARD",
    "RETURN",
    "INTERNAL_NOTE",
    "REQUEST_INFO",
    "FINAL_REPLY",
}

FINALIZE_ACTIONS = {"APPROVE", "FINAL_REPLY", "CLOSE", "ARCHIVE"}

OPEN_STATUSES = {
    "DRAFT",
    "RECEIVED",
    "IN_PROGRESS",
    "FORWARDED",
    "WAITING_INFO",
    "RETURNED",
}


def can_access_correspondence(
    *,
    confidentiality: str | None,
    user_id: int | None,
    has_regular_read: bool,
    has_confidential_read: bool = False,
    has_confidential_manage: bool = False,
    created_by_user_id: int | None = None,
    current_assignee_user_id: int | None = None,
    authorized_user_ids: set[int] | None = None,
) -> bool:
    """Return whether a user may view a correspondence item.

    Regular correspondence follows the module's normal read permission. Secret
    correspondence is intentionally stricter and can only be viewed by its
    creator, current direct assignee, explicitly authorized users, or holders
    of the dedicated confidential read/manage permissions.
    """
    if not user_id:
        return False

    if (confidentiality or "NORMAL").strip().upper() != "SECRET":
        return bool(has_regular_read)

    if has_confidential_read or has_confidential_manage:
        return True

    uid = int(user_id)
    if created_by_user_id and uid == int(created_by_user_id):
        return True
    if current_assignee_user_id and uid == int(current_assignee_user_id):
        return True
    return uid in {int(value) for value in (authorized_user_ids or set()) if value}


def next_status(current_status: str | None, action: str) -> str:
    """Return the status produced by an action; notes do not change status."""
    current = (current_status or "RECEIVED").strip().upper()
    return ACTION_TRANSITIONS.get((action or "").strip().upper(), current)


def parse_iso_date(value: str | None) -> date | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def due_state(value: str | None, today: date | None = None) -> str | None:
    due = parse_iso_date(value)
    if due is None:
        return None
    today = today or date.today()
    if due < today:
        return "OVERDUE"
    if due == today:
        return "DUE_TODAY"
    if (due - today).days <= 3:
        return "DUE_SOON"
    return "SCHEDULED"


def queue_matches(
    *,
    queue: str,
    kind: str,
    status: str | None,
    mail_scope: str | None,
    priority: str | None,
    confidentiality: str | None,
    assigned_to_user_id: int | None,
    current_user_id: int,
    forwarded_by_user_ids: set[int] | None = None,
) -> bool:
    """Pure queue matcher used by the correspondence work dashboard."""
    queue = (queue or "waiting_action").strip().lower()
    kind = (kind or "").strip().upper()
    status = (status or "RECEIVED").strip().upper()
    mail_scope = (mail_scope or "EXTERNAL").strip().upper()
    priority = (priority or "NORMAL").strip().upper()
    confidentiality = (confidentiality or "NORMAL").strip().upper()

    if queue == "incoming":
        return kind == "IN"
    if queue == "outgoing":
        return kind == "OUT"
    if queue == "internal":
        return mail_scope == "INTERNAL"
    if queue == "to_me":
        return assigned_to_user_id == current_user_id
    if queue == "from_me":
        return current_user_id in (forwarded_by_user_ids or set())
    if queue == "waiting_action":
        return status in OPEN_STATUSES
    if queue == "waiting_approval":
        return status == "WAITING_APPROVAL"
    if queue == "completed":
        return status in {"APPROVED", "COMPLETED", "CLOSED"}
    if queue == "archived":
        return status == "ARCHIVED"
    if queue == "returned":
        return status == "RETURNED"
    if queue == "high_priority":
        return priority in {"HIGH", "URGENT"}
    if queue == "confidential":
        return confidentiality == "SECRET"
    return False
