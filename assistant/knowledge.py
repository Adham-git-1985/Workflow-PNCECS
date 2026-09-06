"""Permission-scoped knowledge retrieval for ``اسأل عارف``.

The assistant never receives unrestricted database access.  Every source in
this module is read-only, deliberately curated, and filtered with the same
role, permission, hierarchy, and confidentiality checks used by the product.
"""

from __future__ import annotations

from collections import Counter
from typing import Any
import re
import unicodedata

from flask import current_app, url_for
from sqlalchemy import or_

from models import (
    AuditLog,
    Department,
    InboundMail,
    Notification,
    OutboundMail,
    PortalCircular,
    User,
    WorkflowRequest,
    WorkflowTemplate,
)
from services.circulars import can_user_view_circular, visible_circulars_query
from services.correspondence_workflow import correspondence_target_user_ids
from services.workflow_confidentiality import can_user_access_correspondence_item
from utils.audit_story import build_audit_story_entries
from .project_knowledge import collect_internal_knowledge


_AR_DIACRITICS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
_SPACE = re.compile(r"\s+")
_RECORD_ID = re.compile(r"(?:#\s*|رقم\s+)(\d+)")
_AUDIT_EVENT_LIMIT = 18

_STATUS_LABELS = {
    "DRAFT": "مسودة",
    "PENDING": "قيد الإجراء",
    "IN_PROGRESS": "قيد التنفيذ",
    "APPROVED": "معتمد",
    "REJECTED": "مرفوض",
    "COMPLETED": "مكتمل",
    "CANCELLED": "ملغى",
    "RECEIVED": "مستلم",
    "REGISTERED": "مسجل",
    "ROUTED": "محوّل",
    "FORWARDED": "محوّل",
    "WAITING_APPROVAL": "بانتظار الاعتماد",
    "WAITING_INFO": "بانتظار معلومات",
    "RETURNED": "معاد للمعالجة",
    "SENT": "مرسل",
    "CLOSED": "مغلق",
    "ARCHIVED": "مؤرشف",
}

_STOP_WORDS = {
    "اريد", "بدي", "اعطني", "اعطيني", "اخبرني", "معلومات", "بيانات",
    "عن", "حول", "ما", "هي", "هو", "من", "في", "الى", "على", "هل", "او",
    "ابحث", "بحث", "اظهر", "اعرض", "الموظف", "موظف", "المستخدم",
    "مستخدم", "الطلب", "طلب", "المعامله", "معامله", "الوارد", "وارد",
    "الصادر", "صادر", "المراسلات", "مراسلات", "رقم",
}

_AUDIT_STOP_WORDS = _STOP_WORDS | {
    "سجل", "السجل", "تدقيق", "التدقيق", "زمني", "الزمني", "خط", "الخط",
    "اعمال", "الأعمال", "عمل", "قام", "نفذ", "نفذوا", "منفذ", "ماذا",
    "اصدر", "اصدار", "انشا", "انشاء", "نشر", "رفع", "اضاف", "اضافة",
    "اخر", "آخر", "اجراء", "الإجراء", "اجراءات", "الإجراءات", "تاريخ",
    "تعميم", "التعميم", "تعاميم", "التعاميم", "مسار", "المسار",
    "مراسلة", "المراسلة", "مراسلات", "المراسلات", "خطاب", "الخطاب",
    "كتاب", "الكتاب", "طلب", "الطلب", "طلبات", "الطلبات",
}


def _norm(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = _AR_DIACRITICS.sub("", text).replace("ـ", "")
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    text = text.replace("ى", "ي").replace("ؤ", "و").replace("ئ", "ي").replace("ة", "ه")
    return _SPACE.sub(" ", text).casefold().strip()


_AUDIT_STOP_WORDS_NORMALIZED = {_norm(word) for word in _AUDIT_STOP_WORDS}


def _contains(text: str, *phrases: str) -> bool:
    return any(_norm(phrase) in text for phrase in phrases)


def _compact(value: Any, limit: int = 240) -> str:
    text = _SPACE.sub(" ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _has_role(user, *roles: str) -> bool:
    for role in roles:
        try:
            if user.has_role(role):
                return True
        except Exception:
            continue
    return False


def _has_perm(user, *permissions: str) -> bool:
    for permission in permissions:
        try:
            if user.has_perm(permission):
                return True
        except Exception:
            continue
    return False


def assistant_access_profile(user) -> dict[str, str]:
    """Return the assistant's effective access tier for a user.

    This is intentionally a mirror, never an elevation.  Fine-grained module
    access is still decided by ``has_perm`` at each data source.
    """
    if _has_role(user, "SUPER_ADMIN", "SUPERADMIN"):
        level = "super_admin"
        label = "نطاق سوبر أدمن"
    elif _has_role(user, "ADMIN"):
        level = "admin"
        label = "نطاق أدمن"
    else:
        level = "employee"
        label = "نطاق المستخدم وصلاحياته"
    return {
        "level": level,
        "label": label,
        "role": _compact(getattr(user, "role", None) or "بدون دور", 80),
    }


def _safe_link(endpoint: str, title: str, desc: str, **values) -> dict[str, str] | None:
    try:
        href = url_for(endpoint, **values)
    except Exception:
        return None
    if not href or not str(href).startswith("/"):
        return None
    return {
        "title": _compact(title, 100),
        "desc": _compact(desc, 180),
        "category": "اسأل عارف",
        "href": str(href),
    }


def _department_for_user(user) -> Department | None:
    department_id = getattr(user, "department_id", None)
    if not department_id:
        return None
    try:
        return Department.query.get(int(department_id))
    except Exception:
        return None


def _department_name(user) -> str:
    department = _department_for_user(user)
    if not department:
        return "غير محددة"
    return _compact(department.name_ar or department.name_en or department.code or "غير محددة", 120)


def _status_label(value: Any) -> str:
    raw = str(value or "").strip().upper()
    return _STATUS_LABELS.get(raw, raw or "غير محدد")


def _meaningful_tokens(message: str) -> list[str]:
    tokens = []
    for token in re.findall(r"[0-9A-Za-z\u0600-\u06FF@._-]+", _norm(message)):
        if len(token) < 2 or token in _STOP_WORDS or token.isdigit():
            continue
        tokens.append(token)
    return tokens[:5]


def _extract_record_id(message: str) -> int | None:
    normalized = _norm(message)
    match = _RECORD_ID.search(normalized)
    if not match and normalized.isdigit():
        try:
            return int(normalized)
        except ValueError:
            return None
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _is_guidance_question(normalized: str) -> bool:
    return _contains(
        normalized,
        "كيف", "طريقه", "خطوات", "اين", "وين", "ماذا افعل", "كيفيه",
    )


def _can_view_workflow(user, request_obj: WorkflowRequest) -> bool:
    try:
        # Reuse the exact Workflow visibility gate, including secret-source ACLs,
        # participants, completed-step followers, mentions, and parallel tasks.
        from workflow.routes import _user_can_view_request

        return bool(_user_can_view_request(user, request_obj))
    except Exception:
        current_app.logger.exception("Aref workflow visibility check failed")
        return bool(
            getattr(request_obj, "requester_id", None) == getattr(user, "id", None)
            or assistant_access_profile(user)["level"] == "super_admin"
        )


def _visible_workflows(user, *, limit: int = 300) -> list[WorkflowRequest]:
    profile = assistant_access_profile(user)
    if profile["level"] == "super_admin":
        return (
            WorkflowRequest.query
            .order_by(WorkflowRequest.id.desc())
            .limit(limit)
            .all()
        )

    own = (
        WorkflowRequest.query
        .filter(WorkflowRequest.requester_id == int(user.id))
        .order_by(WorkflowRequest.id.desc())
        .limit(limit)
        .all()
    )
    recent = (
        WorkflowRequest.query
        .order_by(WorkflowRequest.id.desc())
        .limit(limit)
        .all()
    )
    candidates = {int(req.id): req for req in own + recent}
    return [
        req for req in sorted(candidates.values(), key=lambda item: int(item.id), reverse=True)
        if _can_view_workflow(user, req)
    ]


def _workflow_section(user, message: str, *, broad: bool) -> tuple[str, list[str], list[dict[str, str]]]:
    normalized = _norm(message)
    if _is_audit_question(normalized):
        # The audit loader below owns temporal questions so a circular/request
        # number is never accidentally resolved as a different record type.
        return "", [], []
    record_id = _extract_record_id(message)
    explicit = _contains(
        normalized,
        "طلباتي", "الطلبات التي تخصني", "حاله طلبي", "متابعه طلبي",
        "معامله رقم", "طلب رقم", "مهمتي", "مهامي", "صندوق الوارد", "قيد الاجراء",
    ) or record_id is not None
    if not explicit and not broad:
        return "", [], []
    if explicit and _is_guidance_question(normalized) and record_id is None and not _contains(
        normalized, "حاله", "كم", "ما هي", "تخصني", "غير منجزه", "قيد الاجراء",
    ):
        return "", [], []

    visible = _visible_workflows(user)
    if record_id is not None:
        request_obj = WorkflowRequest.query.get(record_id)
        if not request_obj or not _can_view_workflow(user, request_obj):
            return (
                "الطلبات والمسارات\nلا أستطيع عرض هذه المعاملة لأنها غير موجودة أو خارج صلاحياتك.",
                ["تم حجب معاملة غير متاحة للمستخدم."],
                [],
            )
        visible = [request_obj]

    if not visible:
        return "الطلبات والمسارات\nلا توجد معاملات ظاهرة ضمن نطاق صلاحياتك حاليًا.", ["لا توجد معاملات مرئية."], []

    if record_id is not None:
        req = visible[0]
        requester = _compact(getattr(getattr(req, "requester", None), "full_name", None) or "غير محدد", 120)
        request_type = _compact(getattr(getattr(req, "request_type", None), "label", None) or "غير محدد", 120)
        confidentiality = "سري" if (getattr(req, "confidentiality", "NORMAL") or "NORMAL").upper() == "SECRET" else "عادي"
        created_at = getattr(req, "created_at", None)
        lines = [
            f"رقم المعاملة: #{req.id}.",
            f"العنوان: {_compact(req.title or 'دون عنوان', 180)}.",
            f"الحالة: {_status_label(req.status)} — التصنيف: {confidentiality}.",
            f"مقدم الطلب: {requester} — نوع الطلب: {request_type}.",
            f"تاريخ الإنشاء: {created_at.strftime('%Y-%m-%d %H:%M') if created_at else 'غير محدد'}.",
        ]
        if req.description:
            lines.append(f"الوصف: {_compact(req.description, 500)}")
        link = _safe_link(
            "workflow.view_request",
            f"فتح المعاملة #{req.id}",
            f"{_compact(req.title, 120)} — {_status_label(req.status)}",
            request_id=req.id,
        )
        return "تفاصيل المعاملة\n" + "\n".join(lines), lines, [link] if link else []

    status_counts = Counter(_status_label(req.status) for req in visible)
    status_summary = "، ".join(f"{label}: {count}" for label, count in status_counts.most_common(5))
    own_count = sum(1 for req in visible if int(req.requester_id or 0) == int(user.id))
    lines = [f"المعاملات الظاهرة: {len(visible)} (من إنشائك: {own_count}).", f"حسب الحالة: {status_summary}."]
    links: list[dict[str, str]] = []
    for req in visible[:5]:
        confidentiality = " — سري" if (getattr(req, "confidentiality", "NORMAL") or "NORMAL").upper() == "SECRET" else ""
        title = _compact(req.title or f"معاملة #{req.id}", 110)
        lines.append(f"#{req.id} — {title} — {_status_label(req.status)}{confidentiality}")
        link = _safe_link(
            "workflow.view_request",
            f"فتح المعاملة #{req.id}",
            f"{title} — {_status_label(req.status)}",
            request_id=req.id,
        )
        if link:
            links.append(link)
    return "الطلبات والمسارات\n" + "\n".join(lines), lines, links


def _notification_section(user, message: str, *, broad: bool) -> tuple[str, list[str], list[dict[str, str]]]:
    normalized = _norm(message)
    if not _contains(normalized, "اشعار", "اشعارات", "تنبيه", "تنبيهات", "غير مقروء") and not broad:
        return "", [], []

    query = Notification.query.filter(
        Notification.user_id == int(user.id),
        Notification.is_visible.is_(True),
    )
    total = query.count()
    unread = query.filter(Notification.is_read.is_(False)).count()
    latest = query.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(5).all()
    lines = [f"لديك {unread} إشعار غير مقروء من أصل {total}."]
    for item in latest:
        state = "غير مقروء" if not item.is_read else "مقروء"
        lines.append(f"{_compact(item.message, 180)} — {state}")
    link = _safe_link("workflow.notifications", "فتح الإشعارات", "عرض جميع إشعاراتك وتنبيهاتك.")
    return "الإشعارات\n" + "\n".join(lines), lines, [link] if link else []


def _correspondence_matches(item, tokens: list[str], record_id: int | None) -> bool:
    if record_id is not None:
        return int(getattr(item, "id", 0) or 0) == record_id
    if not tokens:
        return True
    haystack = _norm(" ".join(
        str(value or "")
        for value in (
            getattr(item, "ref_no", None),
            getattr(item, "subject", None),
            getattr(item, "sender", None),
            getattr(item, "recipient", None),
            getattr(item, "competence_label", None),
        )
    ))
    return all(token in haystack for token in tokens)


def _correspondence_assigned_to_user(user, item) -> bool:
    """Mirror the portal's direct and organizational assignment check."""
    try:
        user_id = int(user.id)
    except (AttributeError, TypeError, ValueError):
        return False
    try:
        if getattr(item, "current_assignee_id", None) and int(item.current_assignee_id) == user_id:
            return True
    except (TypeError, ValueError):
        pass
    target = {
        "kind": getattr(item, "current_target_kind", None) or getattr(item, "competence_kind", None),
        "id": getattr(item, "current_target_id", None) or getattr(item, "competence_id", None),
        "label": getattr(item, "current_target_label", None) or getattr(item, "competence_label", None),
    }
    try:
        return user_id in set(correspondence_target_user_ids(target))
    except Exception:
        current_app.logger.exception("Aref correspondence assignment check failed")
        return False


def _correspondence_visible_to_user(user, item) -> bool:
    # The correspondence page treats an organizational target as an effective
    # assignee too; use the same rule so Aref never hides a task the user can open.
    return can_user_access_correspondence_item(user, item) or _correspondence_assigned_to_user(user, item)


def _correspondence_section(user, message: str, *, broad: bool) -> tuple[str, list[str], list[dict[str, str]]]:
    normalized = _norm(message)
    if _is_audit_question(normalized):
        return "", [], []
    explicit = _contains(
        normalized,
        "وارد", "صادر", "مراسلات", "مراسله", "كتاب", "كتب", "خطاب", "اجراء", "إجراء", "اعتماد", "تحويل",
    )
    if not explicit and not broad:
        return "", [], []
    record_id = _extract_record_id(message)
    if explicit and _is_guidance_question(normalized) and record_id is None and not _contains(
        normalized, "ابحث", "اعرض", "اظهر", "كم", "اخر", "حاله",
    ):
        return "", [], []
    tokens = _meaningful_tokens(message) if explicit else []
    inbound = InboundMail.query.order_by(InboundMail.id.desc()).limit(250).all()
    outbound = OutboundMail.query.order_by(OutboundMail.id.desc()).limit(250).all()
    inbound = [item for item in inbound if _correspondence_visible_to_user(user, item)]
    outbound = [item for item in outbound if _correspondence_visible_to_user(user, item)]
    matches: list[tuple[str, Any]] = []
    if not _contains(normalized, "صادر") or _contains(normalized, "وارد"):
        matches.extend(("وارد", item) for item in inbound if _correspondence_matches(item, tokens, record_id))
    if not _contains(normalized, "وارد") or _contains(normalized, "صادر"):
        matches.extend(("صادر", item) for item in outbound if _correspondence_matches(item, tokens, record_id))
    matches.sort(key=lambda pair: int(pair[1].id), reverse=True)

    lines = [f"الوارد المتاح: {len(inbound)}، الصادر المتاح: {len(outbound)}."]
    links: list[dict[str, str]] = []
    for kind, item in matches[:6]:
        assigned = _correspondence_assigned_to_user(user, item)
        secrecy = " — سري" if (getattr(item, "confidentiality", "NORMAL") or "NORMAL").upper() == "SECRET" else ""
        assignment = " — ضمن مهامك" if assigned else ""
        reference = _compact(item.ref_no or f"#{item.id}", 60)
        subject = _compact(item.subject, 170)
        lines.append(f"{kind} {reference}: {subject} — {_status_label(item.status)}{secrecy}{assignment}")
        if record_id is not None:
            party = getattr(item, "sender", None) if kind == "وارد" else getattr(item, "recipient", None)
            date_value = getattr(item, "received_date", None) if kind == "وارد" else getattr(item, "sent_date", None)
            if party:
                lines.append(f"الجهة: {_compact(party, 180)}.")
            lines.append(f"التاريخ: {_compact(date_value or 'غير محدد', 40)} — الاستحقاق: {_compact(getattr(item, 'due_date', None) or 'غير محدد', 40)}.")
            target_label = getattr(item, "current_target_label", None) or getattr(item, "competence_label", None)
            if target_label:
                lines.append(f"المكلّف الحالي: {_compact(target_label, 160)}.")
            if getattr(item, "body", None):
                lines.append(f"المحتوى: {_compact(item.body, 500)}")
        endpoint = "portal.inbound_view" if kind == "وارد" else "portal.outbound_view"
        values = {"inbound_id": item.id} if kind == "وارد" else {"outbound_id": item.id}
        if assigned:
            values.update({"focus": "action", "_anchor": "corrActionForm"})
        link_title = f"تنفيذ إجراء {kind} {reference}" if assigned else f"فتح {kind} {reference}"
        link_desc = f"{subject} — انتقل إلى المنطقة المميزة لتنفيذ الإجراء" if assigned else subject
        link = _safe_link(endpoint, link_title, link_desc, **values)
        if link:
            links.append(link)
    if explicit and not matches:
        if not _has_perm(user, "CORR_READ") and not inbound and not outbound:
            lines.append("لا توجد مراسلات مسندة إليك حاليًا، كما أن صلاحية القراءة العامة للمراسلات غير متاحة لحسابك.")
        else:
            lines.append("لم أجد مراسلة مطابقة ضمن البيانات التي تسمح لك صلاحياتك برؤيتها.")
    return "المراسلات\n" + "\n".join(lines), lines, links


def _is_audit_question(normalized: str) -> bool:
    """Whether the user is asking for recorded actions, not general help."""
    return _contains(
        normalized,
        "سجل التدقيق", "سجل تدقيق", "السجل الزمني", "سجل زمني",
        "الخط الزمني", "خط زمني", "التايم لاين", "timeline", "audit log",
        "من عمل على", "من قام على", "من قام بالعمل", "من نفذ", "من تابع",
        "من عدل", "من حوّل", "من حول", "من اصدر", "من نشر", "من انشا",
        "من رفع", "من اضاف", "اخر اجراء", "آخر إجراء",
        "اخر نشاط", "آخر نشاط",
    )


def _audit_tokens(message: str) -> list[str]:
    """Return the object title/reference terms from an audit question only."""
    tokens: list[str] = []
    for token in re.findall(r"[0-9A-Za-z\u0600-\u06FF@._-]+", _norm(message)):
        token = token.strip("،؛؟!.,:;()[]{}\"'")
        # Ignore Arabic prepositions attached to known audit/object terms,
        # e.g. "للتعميم" and "بالمعاملة", without altering real title words.
        for prefix in ("بال", "لل", "ب", "ل"):
            if token.startswith(prefix) and token[len(prefix):] in _AUDIT_STOP_WORDS_NORMALIZED:
                token = token[len(prefix):]
                break
        if len(token) < 2 or token in _AUDIT_STOP_WORDS_NORMALIZED or token.isdigit():
            continue
        if token not in tokens:
            tokens.append(token)
    return tokens[:5]


def _audit_matches(item, tokens: list[str], record_id: int | None, *fields: str) -> bool:
    if record_id is not None:
        return int(getattr(item, "id", 0) or 0) == record_id
    if not tokens:
        return False
    haystack = _norm(" ".join(str(getattr(item, field, None) or "") for field in fields))
    return all(
        token in haystack
        or (len(token) > 3 and token[0] in {"ل", "ب"} and token[1:] in haystack)
        for token in tokens
    )


def _audit_target_kinds(normalized: str) -> tuple[str, ...]:
    """Limit the lookup to the record family the user explicitly named."""
    if _contains(normalized, "تعميم", "تعاميم", "circular"):
        return ("circular",)
    if _contains(normalized, "وارد", "inbound"):
        return ("inbound",)
    if _contains(normalized, "صادر", "outbound"):
        return ("outbound",)
    if _contains(normalized, "مراسلة", "مراسلات", "خطاب", "كتاب"):
        return ("inbound", "outbound")
    if _contains(normalized, "طلب", "معاملة", "مسار", "workflow", "request"):
        return ("workflow",)
    return ("workflow", "circular", "inbound", "outbound")


def _audit_item_link(kind: str, item) -> dict[str, str] | None:
    if kind == "workflow":
        title = _compact(getattr(item, "title", None) or f"معاملة #{item.id}", 110)
        return _safe_link(
            "workflow.view_request",
            f"فتح المعاملة #{item.id}",
            title,
            request_id=item.id,
        )
    if kind == "circular":
        title = _compact(getattr(item, "title", None) or f"تعميم #{item.id}", 110)
        return _safe_link(
            "workflow.circulars_view",
            f"فتح التعميم #{item.id}",
            title,
            circular_id=item.id,
        )
    if kind == "inbound":
        reference = _compact(getattr(item, "ref_no", None) or f"#{item.id}", 60)
        return _safe_link(
            "portal.inbound_view",
            f"فتح الوارد {reference}",
            _compact(getattr(item, "subject", None), 140),
            inbound_id=item.id,
        )
    if kind == "outbound":
        reference = _compact(getattr(item, "ref_no", None) or f"#{item.id}", 60)
        return _safe_link(
            "portal.outbound_view",
            f"فتح الصادر {reference}",
            _compact(getattr(item, "subject", None), 140),
            outbound_id=item.id,
        )
    return None


def _audit_item_title(kind: str, item) -> str:
    if kind == "workflow":
        return _compact(getattr(item, "title", None) or f"معاملة #{item.id}", 160)
    if kind == "circular":
        return _compact(getattr(item, "title", None) or f"تعميم #{item.id}", 160)
    return _compact(getattr(item, "subject", None) or getattr(item, "ref_no", None) or f"#{item.id}", 160)


def _audit_item_label(kind: str) -> str:
    return {
        "workflow": "المعاملة",
        "circular": "التعميم",
        "inbound": "الوارد",
        "outbound": "الصادر",
    }.get(kind, "السجل")


def _audit_candidates(user, message: str) -> list[tuple[str, Any]]:
    """Find only records the requesting user is already allowed to open."""
    normalized = _norm(message)
    record_id = _extract_record_id(message)
    tokens = _audit_tokens(message)
    candidates: list[tuple[str, Any]] = []

    for kind in _audit_target_kinds(normalized):
        if kind == "circular":
            try:
                query = visible_circulars_query(
                    PortalCircular.query,
                    user,
                    include_inactive_for_managers=True,
                )
                rows = (
                    query.filter(PortalCircular.id == record_id).all()
                    if record_id is not None
                    else query.order_by(PortalCircular.created_at.desc(), PortalCircular.id.desc()).limit(300).all()
                )
                candidates.extend(
                    (kind, item)
                    for item in rows
                    if _audit_matches(item, tokens, record_id, "title", "body")
                    and can_user_view_circular(item, user)
                )
            except Exception:
                current_app.logger.exception("Aref circular audit lookup failed")
        elif kind == "workflow":
            try:
                candidates.extend(
                    (kind, item)
                    for item in _visible_workflows(user)
                    if _audit_matches(item, tokens, record_id, "title", "description")
                )
            except Exception:
                current_app.logger.exception("Aref workflow audit lookup failed")
        else:
            try:
                model = InboundMail if kind == "inbound" else OutboundMail
                rows = (
                    model.query.filter(model.id == record_id).all()
                    if record_id is not None
                    else model.query.order_by(model.id.desc()).limit(300).all()
                )
                candidates.extend(
                    (kind, item)
                    for item in rows
                    if _correspondence_visible_to_user(user, item)
                    and _audit_matches(
                        item,
                        tokens,
                        record_id,
                        "ref_no",
                        "subject",
                        "sender",
                        "recipient",
                        "competence_label",
                    )
                )
            except Exception:
                current_app.logger.exception("Aref correspondence audit lookup failed")

    return candidates[:7]


def _audit_logs_for(kind: str, item) -> list[AuditLog]:
    query = AuditLog.query
    if kind == "workflow":
        query = query.filter(AuditLog.request_id == int(item.id))
    else:
        target_type = {
            "circular": "PORTAL_CIRCULAR",
            "inbound": "CORR_INBOUND",
            "outbound": "CORR_OUTBOUND",
        }[kind]
        query = query.filter(
            AuditLog.target_type == target_type,
            AuditLog.target_id == int(item.id),
        )
    return query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(_AUDIT_EVENT_LIMIT).all()


def _audit_actor_name(log: AuditLog) -> str:
    user = getattr(log, "user", None)
    return _compact(
        getattr(user, "full_name", None) or getattr(user, "email", None) or "النظام تلقائيًا",
        100,
    )


def _audit_section(user, message: str, *, broad: bool) -> tuple[str, list[str], list[dict[str, str]]]:
    normalized = _norm(message)
    if not _is_audit_question(normalized):
        return "", [], []

    record_id = _extract_record_id(message)
    tokens = _audit_tokens(message)
    if record_id is None and not tokens:
        return (
            "سجل التدقيق والخط الزمني\n"
            "اكتب رقم المعاملة أو التعميم، أو جزءًا مميزًا من عنوانه، لأعرض من قام بالإجراءات ومتى.",
            ["يلزم رقم أو عنوان مميز للبحث في السجل."],
            [],
        )

    matches = _audit_candidates(user, message)
    if not matches:
        return (
            "سجل التدقيق والخط الزمني\n"
            "لم أجد سجلاً مطابقًا ضمن العناصر التي تسمح لك صلاحياتك برؤيتها. جرّب رقم العنصر أو جزءًا أكثر تمييزًا من عنوانه.",
            ["لا توجد نتيجة تدقيق مرئية ومطابقة."],
            [],
        )

    if len(matches) > 1:
        lines = ["وجدت أكثر من نتيجة. اختر التعميم أو المعاملة المقصودة من القائمة:"]
        links: list[dict[str, str]] = []
        for kind, item in matches:
            label = _audit_item_label(kind)
            title = _audit_item_title(kind, item)
            lines.append(f"{label} #{item.id}: {title}")
            link = _audit_item_link(kind, item)
            if link:
                links.append(link)
        return "سجل التدقيق والخط الزمني\n" + "\n".join(lines), lines, links

    kind, item = matches[0]
    label = _audit_item_label(kind)
    title = _audit_item_title(kind, item)
    logs = _audit_logs_for(kind, item)
    link = _audit_item_link(kind, item)
    links = [link] if link else []
    if not logs:
        lines = [
            f"{label} #{item.id}: {title}.",
            "لا توجد أحداث تدقيق مسجلة لهذا العنصر حتى الآن.",
        ]
        return "سجل التدقيق والخط الزمني\n" + "\n".join(lines), lines, links

    actor_names: list[str] = []
    for log in logs:
        actor = _audit_actor_name(log)
        if actor not in actor_names:
            actor_names.append(actor)

    entries = build_audit_story_entries(logs)
    lines = [
        f"{label} #{item.id}: {title}.",
        f"المستخدمون الذين نفذوا إجراءات ضمن السجل المعروض: {('، '.join(actor_names)) or 'النظام تلقائيًا'}.",
        f"الأحداث المعروضة: {len(entries)} (أحدث الأحداث، بحد أقصى {_AUDIT_EVENT_LIMIT}).",
    ]
    for entry in entries:
        detail = _compact(entry.get("detail"), 260)
        status = _compact(entry.get("status_sentence"), 180)
        text = f"{entry['day_label']} {entry['time_label']} — {entry['sentence']}"
        if status:
            text += f" {status}"
        if detail:
            text += f" ملاحظة: {detail}."
        lines.append(text)
    return "سجل التدقيق والخط الزمني\n" + "\n".join(lines), lines, links


def _directory_scope_query(user, profile: dict[str, str]):
    if profile["level"] in {"super_admin", "admin"}:
        return User.query, "جميع المستخدمين"

    can_read_hr = _has_perm(
        user,
        "HR_EMPLOYEE_READ", "HR_EMPLOYEE_MANAGE", "HR_EMP_READ", "HR_EMP_MANAGE",
    )
    if not can_read_hr:
        return User.query.filter(User.id == int(user.id)), "بياناتك الشخصية فقط"

    raw_role = _norm(getattr(user, "role", None)).replace("-", "_").replace(" ", "_")
    department = _department_for_user(user)
    directorate_id = getattr(user, "directorate_id", None) or getattr(department, "directorate_id", None)
    if raw_role in {"directorate_head", "directorate_deputy", "رئيس_اداره", "نائب_رئيس_اداره"} and directorate_id:
        department_ids = [row.id for row in Department.query.filter(Department.directorate_id == int(directorate_id)).all()]
        return (
            User.query.filter(or_(User.directorate_id == int(directorate_id), User.department_id.in_(department_ids))),
            "الإدارة التابعة لك",
        )
    if getattr(user, "department_id", None):
        return User.query.filter(User.department_id == int(user.department_id)), "الدائرة التابعة لك"
    return User.query.filter(User.id == int(user.id)), "بياناتك الشخصية فقط"


def _directory_section(user, message: str, profile: dict[str, str], *, broad: bool) -> tuple[str, list[str], list[dict[str, str]]]:
    normalized = _norm(message)
    explicit = _contains(
        normalized,
        "موظف", "موظفين", "مستخدم", "مستخدمين", "دليل الموظفين", "من يعمل",
        "البحث عن شخص", "من هو المدير", "من هو الرئيس", "بريد الموظف", "دليل الاتصال",
    )
    if not explicit and not broad:
        return "", [], []
    if explicit and _is_guidance_question(normalized) and not _contains(
        normalized, "ابحث", "اعرض", "اظهر", "كم", "بيانات", "معلومات", "من هو",
    ):
        return "", [], []

    query, scope_label = _directory_scope_query(user, profile)
    scoped_count = query.count()
    tokens = _meaningful_tokens(message) if explicit else []
    if tokens:
        filters = []
        for token in tokens:
            like = f"%{token}%"
            filters.extend((User.name.ilike(like), User.email.ilike(like), User.job_title.ilike(like)))
        query = query.filter(or_(*filters))
    users = query.order_by(User.name.asc(), User.id.asc()).limit(6).all()

    lines = [f"نطاق دليل الموظفين: {scope_label} — العدد: {scoped_count}."]
    links: list[dict[str, str]] = []
    for item in users:
        dept = _department_name(item)
        email = f" — {_compact(item.email, 120)}" if item.email else ""
        lines.append(
            f"{_compact(item.full_name, 100)} — {_compact(item.job_title or 'دون مسمى وظيفي', 100)} — {dept}{email}"
        )
        link = _safe_link(
            "users.profile_view",
            f"ملف {_compact(item.full_name, 80)}",
            f"{_compact(item.job_title or item.role or '', 100)} — {dept}",
            user_id=item.id,
        )
        if link:
            links.append(link)
    if explicit and tokens and not users:
        lines.append("لم أجد موظفًا مطابقًا داخل نطاقك الإداري.")
    return "دليل الموظفين\n" + "\n".join(lines), lines, links


def _account_section(user, message: str, profile: dict[str, str], *, broad: bool) -> tuple[str, list[str], list[dict[str, str]]]:
    normalized = _norm(message)
    explicit = _contains(normalized, "من انا", "بياناتي", "ملفي", "حسابي", "دوري", "صلاحيتي", "صلاحياتي", "ماذا استطيع")
    if not explicit and not broad:
        return "", [], []

    lines = [
        f"الاسم: {_compact(getattr(user, 'full_name', None) or getattr(user, 'name', None) or '', 120)}.",
        f"البريد: {_compact(getattr(user, 'email', None) or 'غير محدد', 160)}.",
        f"الدور: {profile['role']} — {profile['label']}.",
        f"المسمى الوظيفي: {_compact(getattr(user, 'job_title', None) or 'غير محدد', 120)}.",
        f"الدائرة: {_department_name(user)}.",
        "عارف يقرأ ويشرح فقط ضمن صلاحيات هذا الحساب، ولا يرفع الصلاحية أو يتجاوز السرية.",
    ]
    try:
        from utils.system_search import visible_items_for_user

        visible_features = visible_items_for_user(user)
        feature_titles = []
        for item in visible_features:
            title = _compact(getattr(item, "title", None), 80)
            if title and title not in feature_titles:
                feature_titles.append(title)
        if feature_titles:
            examples = "، ".join(feature_titles[:8])
            lines.append(f"الوظائف والشاشات المتاحة: {len(feature_titles)}؛ منها: {examples}.")
    except Exception:
        current_app.logger.exception("Aref feature visibility summary failed")
    link = _safe_link("users.profile", "فتح ملفي الشخصي", "راجع بيانات حسابك وصورتك وكلمة المرور.")
    return "حسابك ونطاق عارف\n" + "\n".join(lines), lines, [link] if link else []


def _system_section(user, message: str, profile: dict[str, str], *, broad: bool) -> tuple[str, list[str], list[dict[str, str]]]:
    normalized = _norm(message)
    explicit = _contains(normalized, "احصائيات النظام", "ملخص النظام", "حاله النظام", "معلومات النظام", "كم عدد")
    if not explicit and not broad:
        return "", [], []
    if profile["level"] not in {"super_admin", "admin"}:
        if explicit:
            return "النظام\nالإحصاءات العامة غير متاحة ضمن صلاحياتك، لكن يمكنني عرض بياناتك ومهامك وإشعاراتك.", ["إحصاءات الإدارة محجوبة."], []
        return "", [], []

    lines = [
        f"المستخدمون: {User.query.count()}.",
        f"المعاملات: {WorkflowRequest.query.count()}.",
        f"قوالب المسارات: {WorkflowTemplate.query.count()}.",
        f"الوارد: {InboundMail.query.count()}، الصادر: {OutboundMail.query.count()}.",
        f"الدوائر: {Department.query.count()}.",
    ]
    return "ملخص النظام الإداري\n" + "\n".join(lines), lines, []


def _dedupe_links(links: list[dict[str, str]]) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in links:
        href = str(item.get("href") or "")
        if not href or href in seen:
            continue
        seen.add(href)
        output.append(item)
        if len(output) >= 6:
            break
    return output


def collect_knowledge(user, message: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Collect permission-filtered live data plus relevant project evidence."""
    context = context or {}
    normalized = _norm(message)
    broad = _contains(
        normalized,
        "ملخص شامل", "ملخص حسابي", "كل معلوماتي", "كل المعلومات", "جميع المعلومات",
        "اعطني ملخص", "ماذا لدي اليوم",
    )
    profile = assistant_access_profile(user)
    sections: list[str] = []
    facts: list[str] = []
    links: list[dict[str, str]] = []
    intents: list[str] = []
    sources: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []

    knowledge_loaders = (
        ("account", lambda: _account_section(user, message, profile, broad=broad)),
        ("system", lambda: _system_section(user, message, profile, broad=broad)),
        ("audit_timeline", lambda: _audit_section(user, message, broad=broad)),
        ("workflow", lambda: _workflow_section(user, message, broad=broad)),
        ("notifications", lambda: _notification_section(user, message, broad=broad)),
        ("correspondence", lambda: _correspondence_section(user, message, broad=broad)),
        ("directory", lambda: _directory_section(user, message, profile, broad=broad)),
    )
    for intent, loader in knowledge_loaders:
        try:
            section, section_facts, section_links = loader()
        except Exception:
            current_app.logger.exception("Aref knowledge source failed: %s", intent)
            continue
        if not section:
            continue
        intents.append(intent)
        sections.append(section)
        facts.extend(_compact(item, 300) for item in section_facts)
        links.extend(item for item in section_links if item)

    # Administrators may ask how the application itself works.  This source is
    # deliberately last: normal user/data questions continue to use the
    # product's fine-grained live-data gates above, while code/schema questions
    # receive locally retrieved, line-addressable evidence.
    try:
        internal = collect_internal_knowledge(user, message, context)
    except Exception:
        current_app.logger.exception("Aref internal project knowledge failed")
        internal = {}
    if internal.get("reply"):
        sections.append(str(internal["reply"]))
        facts.extend(_compact(item, 520) for item in (internal.get("facts") or []))
        links.extend(item for item in (internal.get("links") or []) if item)
        sources.extend(item for item in (internal.get("sources") or []) if item)
        evidence.extend(item for item in (internal.get("evidence") or []) if item)
        intents.extend(str(item) for item in (internal.get("intents") or []) if item)

    return {
        "reply": ("\n\n".join(sections)[:7200].rstrip() if sections else ""),
        "facts": facts[:60],
        "links": _dedupe_links(links),
        "sources": sources[:14],
        "evidence": evidence[:14],
        "intents": intents,
        "access_level": profile["level"],
        "access_label": profile["label"],
        "role": profile["role"],
        "page": _compact(context.get("title"), 120),
        "index_stats": internal.get("index_stats") if internal else None,
    }
