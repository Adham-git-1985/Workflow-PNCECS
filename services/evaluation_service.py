from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import re
from typing import Any

from sqlalchemy import and_, func, or_

from extensions import db
from models import (
    AuditLog,
    ArchivedFile,
    AttendanceDailySummary,
    EmployeeEvaluationRun,
    EmployeeFile,
    HRLeaveRequest,
    HRMonthlyPermissionAllowance,
    HRPermissionRequest,
    PortalMeeting,
    PortalMeetingParticipant,
    PortalMeetingTask,
    SystemSetting,
    User,
    WorkflowRequest,
)
from utils.importer import pick, read_excel_rows, to_int, to_str
from utils.scoring import clamp, score_5_from_100


FINAL_STATUSES = ["APPROVED", "REJECTED"]

ATTENDANCE_CREDIT = {
    "ATTENDED": 1.0,
    "ACCEPTED": 0.8,
    "EXCUSED": 0.7,
    "PROPOSED": 0.6,
    "INVITED": 0.25,
    "DECLINED": 0.25,
    "ABSENT": 0.0,
}

MEETING_ATTENDANCE_LABELS = {
    "INVITED": "مدعو",
    "ACCEPTED": "قبل الدعوة",
    "DECLINED": "رفض الدعوة",
    "PROPOSED": "اقترح موعدا جديدا",
    "ATTENDED": "حضر",
    "ABSENT": "غائب",
    "EXCUSED": "معتذر",
}

MEETING_TASK_STATUS_LABELS = {
    "OPEN": "مفتوحة",
    "IN_PROGRESS": "قيد التنفيذ",
    "DONE": "منجزة",
    "CANCELLED": "ملغاة",
}

APPROVED_HR_STATUSES = {"APPROVED", "COMPLETED"}


def _get_setting_int(key: str, default: int) -> int:
    s = SystemSetting.query.filter_by(key=key).first()
    try:
        return int(s.value) if s and s.value is not None else default
    except Exception:
        return default


def get_sla_days_default() -> int:
    return _get_setting_int("SLA_DAYS", 3)


def _period_type(value: str | None) -> str:
    raw = (value or "").strip()
    upper = raw.upper()
    if upper in ("MONTHLY", "MONTH", "M") or raw in ("شهري", "شهر"):
        return "MONTHLY"
    if upper in ("ANNUAL", "YEARLY", "YEAR", "Y") or raw in ("سنوي", "سنة", "عام"):
        return "ANNUAL"
    raise ValueError("period_type must be MONTHLY or ANNUAL")


def _period_range(period_type: str, year: int, month: int | None) -> tuple[datetime, datetime]:
    period_type = _period_type(period_type)
    if period_type == "MONTHLY":
        if not month or month < 1 or month > 12:
            raise ValueError("month is required for MONTHLY")
        start = datetime(year, month, 1)
        if month == 12:
            end = datetime(year + 1, 1, 1)
        else:
            end = datetime(year, month + 1, 1)
        return start, end

    start = datetime(year, 1, 1)
    end = datetime(year + 1, 1, 1)
    return start, end


def _observed_end(start: datetime, end: datetime) -> datetime:
    """Do not penalize future meetings/tasks when running the current period early."""
    now = datetime.utcnow()
    if now <= start:
        return start
    return min(end, now)


def _inclusive_day(end_exclusive: datetime) -> Any:
    if end_exclusive <= datetime.min + timedelta(days=1):
        return end_exclusive.date()
    return (end_exclusive - timedelta(microseconds=1)).date()


def _norm_count(count: int, target: int) -> float:
    if target <= 0:
        return 0.0
    return clamp(count / float(target), 0.0, 1.0)


def _as_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        if isinstance(value, str):
            value = value.strip().replace("%", "").replace(",", ".")
            if not value:
                return default
        return float(value)
    except Exception:
        return default


def _parse_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if isinstance(value, str):
            value = value.strip().replace("%", "").replace(",", ".")
            if not value:
                return None
        return float(value)
    except Exception:
        return None


def _employee_label(user: User | None) -> str:
    if not user:
        return ""
    return (
        getattr(getattr(user, "employee_file", None), "full_name_quad", None)
        or getattr(user, "full_name", None)
        or getattr(user, "name", None)
        or getattr(user, "username", None)
        or getattr(user, "email", None)
        or f"User #{getattr(user, 'id', '')}"
    )


def _component(
    *,
    label: str,
    description: str,
    weight: float,
    value: Any,
    norm: float,
    explanation: str | None = None,
    details: list[dict[str, Any]] | None = None,
    source: str = "system",
) -> dict[str, Any]:
    return {
        "label": label,
        "description": description,
        "weight": weight,
        "value": value,
        "norm": clamp(norm, 0.0, 1.0),
        "explanation": explanation or description,
        "details": details or [],
        "source": source,
    }


def _finalize_components(components: dict[str, dict[str, Any]]) -> tuple[float, float]:
    total_weight = sum(max(_as_float(c.get("weight"), 0.0), 0.0) for c in components.values()) or 1.0
    score_100 = 0.0

    for c in components.values():
        weight = max(_as_float(c.get("weight"), 0.0), 0.0)
        norm = clamp(_as_float(c.get("norm"), 0.0), 0.0, 1.0)
        effective_weight = weight / total_weight
        indicator_score_100 = round(norm * 100.0, 2)
        score_part = 100.0 * effective_weight * norm

        c["weight"] = weight
        c["norm"] = norm
        c["indicator_score_100"] = indicator_score_100
        c["score_5"] = score_5_from_100(indicator_score_100)
        c["effective_weight"] = round(100.0 * effective_weight, 2)
        c["score_part"] = round(score_part, 2)
        score_100 += score_part

    score_100 = round(clamp(score_100, 0.0, 100.0), 2)
    return score_100, score_5_from_100(score_100)


def _activity_module_label(action: str | None, target_type: str | None) -> str:
    text = f"{action or ''} {target_type or ''}".upper()
    if "MEETING" in text:
        return "الاجتماعات"
    if "ARCHIVE" in text:
        return "الأرشيف"
    if "CORR" in text or "INBOUND" in text or "OUTBOUND" in text:
        return "المراسلات"
    if "HR_" in text or "EMPLOYEE" in text or "LEAVE" in text or "PERMISSION" in text:
        return "الموارد البشرية"
    if "STORE" in text or "INV_" in text:
        return "المستودع"
    if "TRANSPORT" in text:
        return "النقل"
    if "WORKFLOW" in text or "REQUEST" in text:
        return "المسارات"
    if "PAYSLIP" in text:
        return "قسائم الرواتب"
    return "أخرى"


def _limited(items: list[dict[str, Any]], limit: int = 40) -> list[dict[str, Any]]:
    if len(items) <= limit:
        return items
    clipped = items[:limit]
    clipped.append({"ملاحظة": f"تم عرض أول {limit} سجل من أصل {len(items)}."})
    return clipped


def _date_text(value: Any) -> str:
    if not value:
        return ""
    try:
        if hasattr(value, "strftime"):
            return value.strftime("%Y-%m-%d %H:%M") if hasattr(value, "hour") else value.strftime("%Y-%m-%d")
    except Exception:
        pass
    return str(value)


def _parse_ymd(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except Exception:
        return None


def _date_span_days(start_day: date | None, end_day: date | None) -> int:
    if not start_day or not end_day:
        return 0
    if end_day < start_day:
        return 0
    return (end_day - start_day).days + 1


def _overlap_days(row_start: Any, row_end: Any, period_start: date, period_end: date) -> int:
    start_day = _parse_ymd(row_start)
    end_day = _parse_ymd(row_end) or start_day
    if not start_day or not end_day:
        return 0
    return _date_span_days(max(start_day, period_start), min(end_day, period_end))


def _overlap_day_values(row_start: Any, row_end: Any, period_start: date, period_end: date) -> set[str]:
    start_day = _parse_ymd(row_start)
    end_day = _parse_ymd(row_end) or start_day
    if not start_day or not end_day:
        return set()
    first = max(start_day, period_start)
    last = min(end_day, period_end)
    if last < first:
        return set()
    values: set[str] = set()
    cursor = first
    while cursor <= last:
        values.add(cursor.isoformat())
        cursor += timedelta(days=1)
    return values


def _period_months(start_day: date, end_day: date) -> list[tuple[int, int]]:
    months: list[tuple[int, int]] = []
    cursor = date(start_day.year, start_day.month, 1)
    last = date(end_day.year, end_day.month, 1)
    while cursor <= last:
        months.append((cursor.year, cursor.month))
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return months


def _leave_details(leaves: list[HRLeaveRequest], period_start: date, period_end: date) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for leave in leaves:
        leave_type = getattr(leave, "leave_type", None)
        rows.append({
            "رقم الإجازة": getattr(leave, "id", ""),
            "نوع الإجازة": getattr(leave_type, "name_ar", None) or getattr(leave_type, "name_en", None) or getattr(leave_type, "code", "") or "",
            "من": getattr(leave, "start_date", "") or "",
            "إلى": getattr(leave, "end_date", "") or "",
            "أيام ضمن الفترة": _overlap_days(getattr(leave, "start_date", None), getattr(leave, "end_date", None), period_start, period_end),
            "الحالة": getattr(leave, "status", "") or "",
            "ملاحظة": getattr(leave, "note", "") or "",
        })
    return _limited(rows)


def _permission_details(permissions: list[HRPermissionRequest]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for perm in permissions:
        perm_type = getattr(perm, "permission_type", None)
        rows.append({
            "رقم المغادرة": getattr(perm, "id", ""),
            "نوع المغادرة": getattr(perm_type, "name_ar", None) or getattr(perm_type, "name_en", None) or getattr(perm_type, "code", "") or "",
            "اليوم": getattr(perm, "day", "") or "",
            "من": getattr(perm, "from_time", "") or "",
            "إلى": getattr(perm, "to_time", "") or "",
            "الساعات": getattr(perm, "hours", 0) or 0,
            "الحالة": getattr(perm, "status", "") or "",
            "ملاحظة": getattr(perm, "note", "") or "",
        })
    return _limited(rows)


def _attendance_exception_details(rows_in: list[AttendanceDailySummary]) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for row in rows_in:
        status = (getattr(row, "status", "") or "").upper()
        late = int(getattr(row, "late_minutes", 0) or 0)
        early = int(getattr(row, "early_leave_minutes", 0) or 0)
        if status in {"OK", ""} and late <= 0 and early <= 0:
            continue
        details.append({
            "اليوم": getattr(row, "day", "") or "",
            "الحالة": status or "OK",
            "تأخير بالدقائق": late,
            "خروج مبكر بالدقائق": early,
            "دقائق عمل": int(getattr(row, "work_minutes", 0) or 0),
        })
    return _limited(details)


def _meeting_details(participants: list[PortalMeetingParticipant]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in participants:
        meeting = getattr(p, "meeting", None)
        rows.append({
            "رقم الاجتماع": getattr(meeting, "id", ""),
            "العنوان": getattr(meeting, "title", ""),
            "التاريخ": _date_text(getattr(meeting, "start_at", None)),
            "حالة الاجتماع": getattr(meeting, "status", ""),
            "الدور": getattr(p, "role", ""),
            "الحضور": MEETING_ATTENDANCE_LABELS.get(getattr(p, "attendance_status", ""), getattr(p, "attendance_status", "")),
            "ملاحظة": getattr(p, "note", "") or "",
        })
    return _limited(rows)


def _organized_meeting_details(meetings: list[PortalMeeting]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for meeting in meetings:
        agenda_count = len(getattr(meeting, "agenda_items", None) or [])
        task_count = len(getattr(meeting, "tasks", None) or [])
        has_minutes = bool((getattr(meeting, "minutes_text", None) or "").strip())
        has_decisions = bool((getattr(meeting, "decisions_text", None) or "").strip())
        rows.append({
            "رقم الاجتماع": getattr(meeting, "id", ""),
            "العنوان": getattr(meeting, "title", ""),
            "التاريخ": _date_text(getattr(meeting, "start_at", None)),
            "الحالة": getattr(meeting, "status", ""),
            "بنود الأجندة": agenda_count,
            "مهام المتابعة": task_count,
            "محضر": "نعم" if has_minutes else "لا",
            "قرارات": "نعم" if has_decisions else "لا",
        })
    return _limited(rows)


def _task_details(tasks: list[PortalMeetingTask]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for task in tasks:
        meeting = getattr(task, "meeting", None)
        due_date = getattr(task, "due_date", None)
        completed_at = getattr(task, "completed_at", None)
        on_time = ""
        if (getattr(task, "status", "") or "").upper() == "DONE":
            on_time = "نعم" if (not due_date or (completed_at and completed_at.date() <= due_date)) else "لا"
        rows.append({
            "رقم المهمة": getattr(task, "id", ""),
            "المهمة": getattr(task, "title", ""),
            "الاجتماع": getattr(meeting, "title", ""),
            "تاريخ التكليف": _date_text(getattr(task, "created_at", None)),
            "تاريخ الاستحقاق": _date_text(due_date),
            "الحالة": MEETING_TASK_STATUS_LABELS.get(getattr(task, "status", ""), getattr(task, "status", "")),
            "تاريخ الإنجاز": _date_text(completed_at),
            "ضمن الوقت": on_time,
        })
    return _limited(rows)


def _build_summary(metrics: dict[str, Any]) -> str:
    summary_bits = [f"الحركات: {metrics.get('total_actions', 0)}"]
    decisions_count = int(metrics.get("approvals", 0) or 0) + int(metrics.get("rejections", 0) or 0)
    if decisions_count:
        summary_bits.append(
            f"قرارات: {decisions_count} (موافقة {metrics.get('approvals', 0)} / رفض {metrics.get('rejections', 0)})"
        )
    if metrics.get("sla_ratio") is not None:
        summary_bits.append(f"الالتزام بالوقت: {int(round(float(metrics['sla_ratio']) * 100))}%")
    files_count = int(metrics.get("archive_uploads", 0) or 0) + int(metrics.get("audit_archive_uploads", 0) or 0)
    if files_count:
        summary_bits.append(f"ملفات مرفوعة: {files_count}")
    if metrics.get("rej_note_ratio") is not None:
        summary_bits.append(f"توثيق الرفض: {int(round(float(metrics['rej_note_ratio']) * 100))}%")
    if metrics.get("meeting_participation_total"):
        summary_bits.append(f"اجتماعات: {metrics.get('meeting_participation_total', 0)}")
    if metrics.get("meeting_tasks_assigned"):
        summary_bits.append(
            f"مهام اجتماع: {metrics.get('meeting_tasks_done', 0)}/{metrics.get('meeting_tasks_assigned', 0)}"
        )
    if metrics.get("attendance_records") or metrics.get("approved_leave_days") or metrics.get("approved_permission_hours"):
        summary_bits.append(
            "الانضباط: "
            f"غياب {metrics.get('unauthorized_absence_days', 0)} يوم، "
            f"تأخير {metrics.get('late_minutes', 0)} دقيقة، "
            f"مغادرات {metrics.get('approved_permission_hours', 0)} ساعة"
        )
    if metrics.get("imported_indicators"):
        summary_bits.append(f"مؤشرات مستوردة: {metrics.get('imported_indicators', 0)}")
    return " | ".join(summary_bits)


def _base_breakdown(user: User, period_type: str, year: int, month: int | None, start: datetime, end: datetime) -> dict[str, Any]:
    return {
        "employee": {"id": user.id, "name": _employee_label(user)},
        "period": {
            "type": period_type,
            "year": year,
            "month": month if period_type == "MONTHLY" else None,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "sla_days": get_sla_days_default(),
        },
        "metrics": {},
        "components": {},
        "score": {"score_100": 0.0, "score_5": 0.0},
        "notes": [
            "الدرجة الأساسية تُحسب من مؤشرات رقمية (KPI) ثم تُحوّل إلى 5 مع تقريب 0.1.",
            "الأوزان تُعاد نسبتها على المؤشرات المتاحة حتى لا يُعاقب الموظف على محور لا ينطبق عليه.",
            "مؤشرات الاجتماعات تُحتسب عند وجود دعوات أو مهام أو اجتماعات منظمة ضمن الفترة فقط.",
            "الإجازات والمغادرات المعتمدة تُعرض وتُحتسب كأثر توفر محدود، بينما الغياب غير المغطى والتأخير والخروج المبكر لها أثر أكبر.",
        ],
    }


def _load_breakdown(run: EmployeeEvaluationRun, user: User, start: datetime, end: datetime) -> dict[str, Any]:
    try:
        data = json.loads(run.breakdown_json) if run.breakdown_json else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("employee", {"id": user.id, "name": _employee_label(user)})
    data.setdefault("period", {
        "type": run.period_type,
        "year": run.year,
        "month": run.month if run.period_type == "MONTHLY" else None,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "sla_days": get_sla_days_default(),
    })
    data.setdefault("metrics", {})
    data.setdefault("components", {})
    data.setdefault("notes", [])
    data.setdefault("score", {"score_100": run.score_100 or 0.0, "score_5": run.score_5 or 0.0})
    return data


def _get_or_create_run(
    user: User,
    period_type: str,
    year: int,
    month: int | None,
    *,
    created_by_id: int | None = None,
) -> EmployeeEvaluationRun:
    start, end = _period_range(period_type, year, month)
    period_type_u = _period_type(period_type)
    month_value = month if period_type_u == "MONTHLY" else None
    run = EmployeeEvaluationRun.query.filter_by(
        user_id=user.id,
        period_type=period_type_u,
        year=year,
        month=month_value,
    ).first()
    if not run:
        run = EmployeeEvaluationRun(
            user_id=user.id,
            period_type=period_type_u,
            year=year,
            month=month_value,
            start_date=start,
            end_date=end,
            score_100=0.0,
            score_5=0.0,
            breakdown_json=json.dumps(_base_breakdown(user, period_type_u, year, month_value, start, end), ensure_ascii=False),
            summary="",
            created_by_id=created_by_id,
        )
        db.session.add(run)
        db.session.flush()
    return run


def compute_employee_evaluation(
    user_id: int,
    period_type: str,
    year: int,
    month: int | None,
    *,
    created_by_id: int | None = None,
) -> EmployeeEvaluationRun:
    """Compute and upsert an EmployeeEvaluationRun for a given period."""

    period_type_u = _period_type(period_type)
    start, end = _period_range(period_type_u, year, month)
    observed_end = _observed_end(start, end)
    observed_day = _inclusive_day(observed_end)
    sla_days = get_sla_days_default()

    u = User.query.get_or_404(user_id)

    # ---------------------------
    # Base counts from AuditLog
    # ---------------------------
    base_audit_q = AuditLog.query.filter(
        AuditLog.created_at >= start,
        AuditLog.created_at < end,
        or_(AuditLog.user_id == user_id, AuditLog.on_behalf_of_id == user_id),
    )

    # exclude message noise
    base_audit_q = base_audit_q.filter(~AuditLog.action.like("MESSAGE_%"))

    total_actions = base_audit_q.count()
    approvals = base_audit_q.filter(AuditLog.action == "APPROVE").count()
    rejections = base_audit_q.filter(AuditLog.action == "REJECT").count()

    activity_by_module: dict[str, int] = {}
    for action, target_type in base_audit_q.with_entities(AuditLog.action, AuditLog.target_type).all():
        label = _activity_module_label(action, target_type)
        activity_by_module[label] = activity_by_module.get(label, 0) + 1

    # compliance proxy: rejection note present
    rej_with_note = base_audit_q.filter(
        AuditLog.action == "REJECT",
        AuditLog.note.isnot(None),
        func.length(func.trim(AuditLog.note)) > 0,
    ).count()
    rej_note_ratio = None
    if rejections > 0:
        rej_note_ratio = clamp(rej_with_note / float(rejections), 0.0, 1.0)

    # SLA proxy: decisions taken within SLA days from request creation
    decisions_q = base_audit_q.filter(AuditLog.action.in_(["APPROVE", "REJECT"]))
    decision_rows = decisions_q.with_entities(AuditLog.request_id, AuditLog.created_at).all()
    on_time = 0
    late = 0
    if decision_rows:
        req_ids = sorted({rid for rid, _ in decision_rows if rid})
        if req_ids:
            req_map = {
                rid: created_at
                for (rid, created_at) in (
                    WorkflowRequest.query
                    .filter(WorkflowRequest.id.in_(req_ids))
                    .with_entities(WorkflowRequest.id, WorkflowRequest.created_at)
                    .all()
                )
            }
            for rid, act_at in decision_rows:
                rc = req_map.get(rid)
                if not rc:
                    continue
                delta = act_at - rc
                if delta <= timedelta(days=sla_days):
                    on_time += 1
                else:
                    late += 1

    sla_ratio = None
    if (on_time + late) > 0:
        sla_ratio = clamp(on_time / float(on_time + late), 0.0, 1.0)

    # ---------------------------
    # Archive uploads (files)
    # ---------------------------
    archive_uploads = ArchivedFile.query.filter(
        ArchivedFile.owner_id == user_id,
        ArchivedFile.upload_date >= start,
        ArchivedFile.upload_date < end,
    ).count()

    # Also include archive uploads logged in audit (if used)
    audit_archive_uploads = base_audit_q.filter(AuditLog.action == "ARCHIVE_UPLOADED").count()

    # ---------------------------
    # Requests created by employee (as requester)
    # ---------------------------
    created_requests = WorkflowRequest.query.filter(
        WorkflowRequest.requester_id == user_id,
        WorkflowRequest.created_at >= start,
        WorkflowRequest.created_at < end,
    ).count()

    created_closed = WorkflowRequest.query.filter(
        WorkflowRequest.requester_id == user_id,
        WorkflowRequest.created_at >= start,
        WorkflowRequest.created_at < end,
        WorkflowRequest.status.in_(FINAL_STATUSES),
    ).count()

    requester_closure_ratio = None
    if created_requests > 0:
        requester_closure_ratio = clamp(created_closed / float(created_requests), 0.0, 1.0)

    # ---------------------------
    # Meetings and follow-up tasks
    # ---------------------------
    participant_rows: list[PortalMeetingParticipant] = []
    organized_meetings: list[PortalMeeting] = []
    task_rows: list[PortalMeetingTask] = []
    eligible_tasks: list[PortalMeetingTask] = []

    if observed_end > start:
        participant_rows = (
            PortalMeetingParticipant.query
            .join(PortalMeeting, PortalMeetingParticipant.meeting_id == PortalMeeting.id)
            .filter(PortalMeetingParticipant.user_id == user_id)
            .filter(PortalMeeting.start_at >= start, PortalMeeting.start_at < observed_end)
            .filter(PortalMeeting.status != "CANCELLED")
            .order_by(PortalMeeting.start_at.asc(), PortalMeeting.id.asc())
            .all()
        )

        organized_meetings = (
            PortalMeeting.query
            .filter(PortalMeeting.created_by_user_id == user_id)
            .filter(PortalMeeting.start_at >= start, PortalMeeting.start_at < observed_end)
            .filter(PortalMeeting.status != "CANCELLED")
            .order_by(PortalMeeting.start_at.asc(), PortalMeeting.id.asc())
            .all()
        )

        task_rows = (
            PortalMeetingTask.query
            .filter(PortalMeetingTask.assignee_user_id == user_id)
            .filter(
                or_(
                    and_(PortalMeetingTask.created_at >= start, PortalMeetingTask.created_at < observed_end),
                    and_(PortalMeetingTask.completed_at >= start, PortalMeetingTask.completed_at < observed_end),
                    and_(PortalMeetingTask.due_date >= start.date(), PortalMeetingTask.due_date <= observed_day),
                )
            )
            .order_by(PortalMeetingTask.created_at.asc(), PortalMeetingTask.id.asc())
            .all()
        )

        for task in task_rows:
            status = (getattr(task, "status", "") or "").upper()
            if status == "CANCELLED":
                continue
            due_date = getattr(task, "due_date", None)
            created_at = getattr(task, "created_at", None)
            completed_at = getattr(task, "completed_at", None)
            is_due = bool(due_date and due_date <= observed_day)
            is_done = status == "DONE"
            has_no_due_but_created = bool(not due_date and created_at and start <= created_at < observed_end)
            completed_in_period = bool(completed_at and start <= completed_at < observed_end)
            if is_due or is_done or has_no_due_but_created or completed_in_period:
                eligible_tasks.append(task)

    meeting_status_counts: dict[str, int] = {}
    meeting_credit_total = 0.0
    for p in participant_rows:
        status = (getattr(p, "attendance_status", "") or "INVITED").upper()
        meeting_status_counts[status] = meeting_status_counts.get(status, 0) + 1
        meeting_credit_total += ATTENDANCE_CREDIT.get(status, 0.25)

    meeting_participation_norm = None
    if participant_rows:
        meeting_participation_norm = clamp(meeting_credit_total / float(len(participant_rows)), 0.0, 1.0)

    organized_count = len(organized_meetings)
    organized_with_agenda = 0
    organized_documented = 0
    for meeting in organized_meetings:
        if getattr(meeting, "agenda_items", None):
            organized_with_agenda += 1
        if (
            (getattr(meeting, "minutes_text", None) or "").strip()
            or (getattr(meeting, "decisions_text", None) or "").strip()
            or (getattr(meeting, "status", "") or "").upper() == "DONE"
        ):
            organized_documented += 1

    organized_norm = None
    if organized_count:
        agenda_ratio = organized_with_agenda / float(organized_count)
        doc_ratio = organized_documented / float(organized_count)
        organized_norm = clamp((agenda_ratio * 0.4) + (doc_ratio * 0.6), 0.0, 1.0)

    meeting_tasks_assigned = len(eligible_tasks)
    meeting_tasks_done = 0
    meeting_tasks_on_time = 0
    for task in eligible_tasks:
        if (getattr(task, "status", "") or "").upper() == "DONE":
            meeting_tasks_done += 1
            due_date = getattr(task, "due_date", None)
            completed_at = getattr(task, "completed_at", None)
            if not due_date or (completed_at and completed_at.date() <= due_date):
                meeting_tasks_on_time += 1

    meeting_tasks_norm = None
    if meeting_tasks_assigned:
        completion_ratio = meeting_tasks_done / float(meeting_tasks_assigned)
        on_time_ratio = (meeting_tasks_on_time / float(meeting_tasks_done)) if meeting_tasks_done else 0.0
        meeting_tasks_norm = clamp((completion_ratio * 0.7) + (on_time_ratio * 0.3), 0.0, 1.0)

    # ---------------------------
    # Attendance, leaves, permissions
    # ---------------------------
    attendance_rows: list[AttendanceDailySummary] = []
    approved_leaves: list[HRLeaveRequest] = []
    approved_permissions: list[HRPermissionRequest] = []
    attendance_records = 0
    absent_days = 0
    incomplete_days = 0
    unauthorized_absence_days = 0
    late_minutes = 0
    early_leave_minutes = 0
    approved_leave_days = 0
    approved_permission_days = 0
    approved_permission_hours = 0.0
    permission_allowance_hours = 0
    excess_permission_hours = 0.0
    attendance_discipline_norm = None

    period_start_day = start.date()
    period_end_day = observed_day
    if observed_end > start and period_end_day >= period_start_day:
        period_start_s = period_start_day.isoformat()
        period_end_s = period_end_day.isoformat()

        attendance_rows = (
            AttendanceDailySummary.query
            .filter(AttendanceDailySummary.user_id == user_id)
            .filter(AttendanceDailySummary.day >= period_start_s)
            .filter(AttendanceDailySummary.day <= period_end_s)
            .order_by(AttendanceDailySummary.day.asc())
            .all()
        )
        attendance_records = len(attendance_rows)
        absence_day_values: set[str] = set()
        for row in attendance_rows:
            status = (getattr(row, "status", "") or "").upper()
            late_minutes += max(int(getattr(row, "late_minutes", 0) or 0), 0)
            early_leave_minutes += max(int(getattr(row, "early_leave_minutes", 0) or 0), 0)
            no_punches = not getattr(row, "first_in", None) and not getattr(row, "last_out", None)
            if status == "ABSENT" or no_punches:
                absent_days += 1
                if getattr(row, "day", None):
                    absence_day_values.add(str(row.day))
            elif status == "INCOMPLETE":
                incomplete_days += 1

        approved_leaves = (
            HRLeaveRequest.query
            .filter(HRLeaveRequest.user_id == user_id)
            .filter(func.upper(HRLeaveRequest.status).in_(APPROVED_HR_STATUSES))
            .filter(HRLeaveRequest.start_date <= period_end_s)
            .filter(HRLeaveRequest.end_date >= period_start_s)
            .order_by(HRLeaveRequest.start_date.asc(), HRLeaveRequest.id.asc())
            .all()
        )
        approved_leave_day_values: set[str] = set()
        for leave in approved_leaves:
            approved_leave_day_values.update(
                _overlap_day_values(getattr(leave, "start_date", None), getattr(leave, "end_date", None), period_start_day, period_end_day)
            )
        approved_leave_days = len(approved_leave_day_values)
        unauthorized_absence_days = len(absence_day_values - approved_leave_day_values)

        approved_permissions = (
            HRPermissionRequest.query
            .filter(HRPermissionRequest.user_id == user_id)
            .filter(func.upper(HRPermissionRequest.status).in_(APPROVED_HR_STATUSES))
            .filter(HRPermissionRequest.day >= period_start_s)
            .filter(HRPermissionRequest.day <= period_end_s)
            .order_by(HRPermissionRequest.day.asc(), HRPermissionRequest.id.asc())
            .all()
        )
        permission_days_seen: set[str] = set()
        for perm in approved_permissions:
            if getattr(perm, "day", None):
                permission_days_seen.add(str(perm.day))
            approved_permission_hours += max(_as_float(getattr(perm, "hours", 0), 0.0), 0.0)
        approved_permission_days = len(permission_days_seen)

        for year_value, month_value in _period_months(period_start_day, period_end_day):
            row = HRMonthlyPermissionAllowance.query.filter_by(
                user_id=user_id,
                year=year_value,
                month=month_value,
            ).first()
            if row:
                permission_allowance_hours += max(int(getattr(row, "allowed_hours", 0) or 0), 0)
        excess_permission_hours = max(approved_permission_hours - float(permission_allowance_hours), 0.0)

        if attendance_records or approved_leave_days or approved_permission_days:
            late_hours = late_minutes / 60.0
            early_hours = early_leave_minutes / 60.0
            approved_leave_penalty = min(approved_leave_days * 0.15, 6.0)
            excess_permission_penalty = excess_permission_hours * 0.7
            penalty = (
                unauthorized_absence_days * 8.0
                + incomplete_days * 3.0
                + late_hours * 1.5
                + early_hours * 1.5
                + approved_leave_penalty
                + excess_permission_penalty
            )
            attendance_discipline_norm = clamp((100.0 - penalty) / 100.0, 0.0, 1.0)

    # ---------------------------
    # Scoring (0..100)
    # ---------------------------
    # Targets: monthly vs annual
    if period_type_u == "ANNUAL":
        tgt_actions = 600
        tgt_decisions = 120
        tgt_files = 120
    else:
        tgt_actions = 50
        tgt_decisions = 10
        tgt_files = 10

    comp: dict[str, dict[str, Any]] = {}

    # Always computable
    comp["activity"] = _component(
        label="النشاط العام داخل النظام",
        description="يشمل الحركات المسجلة في السجل لكل الوحدات، بما فيها المسارات والاجتماعات والأرشيف وأي وحدات مضافة.",
        weight=20,
        value=total_actions,
        norm=_norm_count(total_actions, tgt_actions),
        explanation=f"تم تسجيل {total_actions} حركة خلال الفترة، والهدف المرجعي لهذه الفترة هو {tgt_actions}.",
        details=[{"الوحدة": k, "عدد الحركات": v} for k, v in sorted(activity_by_module.items())],
    )

    # Decisions/productivity (only if employee did decisions)
    decisions_count = approvals + rejections
    if decisions_count > 0:
        comp["decisions"] = _component(
            label="الإنتاجية في القرارات",
            description="عدد قرارات الموافقة أو الرفض التي نفذها الموظف ضمن الفترة.",
            weight=25,
            value={"إجمالي القرارات": decisions_count, "موافقات": approvals, "رفض": rejections},
            norm=_norm_count(decisions_count, tgt_decisions),
            explanation=f"تم احتساب {decisions_count} قرارا مقارنة بهدف مرجعي {tgt_decisions}.",
        )

    # SLA (only if decisions exist and we could compute it)
    if sla_ratio is not None:
        comp["sla"] = _component(
            label="الالتزام بالوقت",
            description="نسبة القرارات التي تمت ضمن المدة المحددة في SLA_DAYS.",
            weight=30,
            value={"ضمن الوقت": on_time, "متأخر": late, "النسبة": sla_ratio},
            norm=sla_ratio,
            explanation=f"{on_time} من أصل {on_time + late} قرارا تمت ضمن {sla_days} أيام.",
        )

    # Documentation / compliance (files or reject-notes or both)
    doc_parts = []
    if archive_uploads or audit_archive_uploads:
        doc_parts.append(_norm_count(archive_uploads + audit_archive_uploads, tgt_files))
    if rej_note_ratio is not None:
        doc_parts.append(rej_note_ratio)

    if doc_parts:
        comp["documentation"] = _component(
            label="التوثيق وجودة السجل",
            description="يقيس رفع الملفات وتوثيق أسباب الرفض عندما توجد قرارات رفض.",
            weight=25,
            value={
                "ملفات الأرشيف": archive_uploads,
                "ملفات مسجلة في السجل": audit_archive_uploads,
                "قرارات الرفض": rejections,
                "رفض مع ملاحظة": rej_with_note,
                "نسبة توثيق الرفض": rej_note_ratio,
            },
            norm=clamp(sum(doc_parts) / float(len(doc_parts)), 0.0, 1.0),
            explanation="تم دمج مؤشرات رفع الملفات وتوثيق الرفض المتاحة لهذه الفترة.",
        )

    # Requester completion (only if user created requests)
    if requester_closure_ratio is not None:
        comp["requester_completion"] = _component(
            label="اكتمال الطلبات المقدمة",
            description="نسبة الطلبات التي أنشأها الموظف ووصلت إلى حالة نهائية ضمن الفترة.",
            weight=10,
            value={
                "طلبات منشأة": created_requests,
                "طلبات مغلقة": created_closed,
                "النسبة": requester_closure_ratio,
            },
            norm=requester_closure_ratio,
            explanation=f"أغلق النظام {created_closed} من أصل {created_requests} طلبا أنشأه الموظف.",
        )

    if meeting_participation_norm is not None:
        comp["meeting_participation"] = _component(
            label="المشاركة في الاجتماعات",
            description="يقيس الاستجابة والحضور للاجتماعات التي دعي إليها الموظف ضمن الفترة.",
            weight=10,
            value={
                "إجمالي الاجتماعات": len(participant_rows),
                "تفصيل الحضور": {
                    MEETING_ATTENDANCE_LABELS.get(k, k): v
                    for k, v in sorted(meeting_status_counts.items())
                },
            },
            norm=meeting_participation_norm,
            explanation="الحضور الكامل يأخذ أعلى قيمة، ثم قبول الدعوة أو الاعتذار/اقتراح الموعد، بينما الغياب أو عدم الرد يخفض المؤشر.",
            details=_meeting_details(participant_rows),
        )

    if organized_norm is not None:
        comp["meeting_organization"] = _component(
            label="تنظيم الاجتماعات وتوثيقها",
            description="يقيس جاهزية الأجندة وتوثيق المحضر أو القرارات للاجتماعات التي نظمها الموظف.",
            weight=10,
            value={
                "اجتماعات منظمة": organized_count,
                "بأجندة": organized_with_agenda,
                "موثقة بمحضر أو قرارات": organized_documented,
            },
            norm=organized_norm,
            explanation="يحسب 40% لجاهزية الأجندة و60% لتوثيق المحضر أو القرارات أو إغلاق الاجتماع.",
            details=_organized_meeting_details(organized_meetings),
        )

    if meeting_tasks_norm is not None:
        comp["meeting_followup_tasks"] = _component(
            label="مهام ما بعد الاجتماع",
            description="يقيس إنجاز مهام المتابعة المكلف بها الموظف من الاجتماعات، مع مراعاة الإنجاز ضمن الوقت.",
            weight=15,
            value={
                "مهام مكلف بها": meeting_tasks_assigned,
                "مهام منجزة": meeting_tasks_done,
                "منجزة ضمن الوقت": meeting_tasks_on_time,
            },
            norm=meeting_tasks_norm,
            explanation="70% من المؤشر يعتمد على نسبة الإنجاز و30% على إنجاز المهام ضمن تاريخ الاستحقاق.",
            details=_task_details(eligible_tasks),
        )

    if attendance_discipline_norm is not None:
        attendance_details = []
        attendance_details.extend(_attendance_exception_details(attendance_rows))
        if approved_leaves:
            attendance_details.append({"قسم": "الإجازات المعتمدة"})
            attendance_details.extend(_leave_details(approved_leaves, period_start_day, period_end_day))
        if approved_permissions:
            attendance_details.append({"قسم": "المغادرات المعتمدة"})
            attendance_details.extend(_permission_details(approved_permissions))

        comp["attendance_discipline"] = _component(
            label="الانضباط والحضور",
            description="يقيس أثر الغياب غير المغطى، التأخير، الخروج المبكر، الإجازات، والمغادرات ضمن الفترة.",
            weight=20,
            value={
                "أيام حضور/سجلات": attendance_records,
                "غياب مسجل": absent_days,
                "غياب غير مغطى بإجازة": unauthorized_absence_days,
                "أيام غير مكتملة": incomplete_days,
                "دقائق التأخير": late_minutes,
                "دقائق الخروج المبكر": early_leave_minutes,
                "أيام الإجازات المعتمدة": approved_leave_days,
                "أيام المغادرات المعتمدة": approved_permission_days,
                "ساعات المغادرات المعتمدة": round(approved_permission_hours, 2),
                "سماح المغادرات الشهري": permission_allowance_hours,
                "ساعات مغادرات فوق السماح": round(excess_permission_hours, 2),
            },
            norm=attendance_discipline_norm,
            explanation=(
                "الغياب غير المغطى والأيام غير المكتملة والتأخير والخروج المبكر تؤثر مباشرة. "
                "الإجازات المعتمدة تُحتسب كأثر توفر خفيف، والمغادرات المعتمدة تُخصم فقط عند تجاوز السماح الشهري المسجل."
            ),
            details=_limited(attendance_details, 80),
        )

    score_100, score_5 = _finalize_components(comp)

    metrics = {
        "total_actions": total_actions,
        "activity_by_module": activity_by_module,
        "approvals": approvals,
        "rejections": rejections,
        "rejections_with_note": rej_with_note,
        "rej_note_ratio": rej_note_ratio,
        "sla_on_time": on_time,
        "sla_late": late,
        "sla_ratio": sla_ratio,
        "archive_uploads": archive_uploads,
        "audit_archive_uploads": audit_archive_uploads,
        "created_requests": created_requests,
        "created_closed": created_closed,
        "requester_closure_ratio": requester_closure_ratio,
        "meeting_participation_total": len(participant_rows),
        "meeting_attendance_counts": meeting_status_counts,
        "meetings_created": organized_count,
        "meetings_with_agenda": organized_with_agenda,
        "meetings_documented": organized_documented,
        "meeting_tasks_assigned": meeting_tasks_assigned,
        "meeting_tasks_done": meeting_tasks_done,
        "meeting_tasks_on_time": meeting_tasks_on_time,
        "attendance_records": attendance_records,
        "absent_days": absent_days,
        "unauthorized_absence_days": unauthorized_absence_days,
        "incomplete_attendance_days": incomplete_days,
        "late_minutes": late_minutes,
        "early_leave_minutes": early_leave_minutes,
        "approved_leave_days": approved_leave_days,
        "approved_permission_days": approved_permission_days,
        "approved_permission_hours": round(approved_permission_hours, 2),
        "permission_allowance_hours": permission_allowance_hours,
        "excess_permission_hours": round(excess_permission_hours, 2),
    }

    breakdown = _base_breakdown(u, period_type_u, year, month if period_type_u == "MONTHLY" else None, start, end)
    breakdown["metrics"] = metrics
    breakdown["components"] = comp
    breakdown["score"] = {"score_100": score_100, "score_5": score_5}

    summary = _build_summary(metrics)

    # Upsert
    run = _get_or_create_run(
        u,
        period_type_u,
        year,
        month if period_type_u == "MONTHLY" else None,
        created_by_id=created_by_id,
    )

    run.start_date = start
    run.end_date = end
    run.score_100 = score_100
    run.score_5 = score_5
    run.breakdown_json = json.dumps(breakdown, ensure_ascii=False)
    run.summary = summary
    run.created_by_id = created_by_id
    run.created_at = datetime.utcnow()

    db.session.commit()
    return run


def compute_for_all_employees(
    period_type: str,
    year: int,
    month: int | None,
    *,
    created_by_id: int | None = None,
) -> int:
    """Compute evaluation for all users."""
    users = User.query.order_by(User.id.asc()).all()
    count = 0
    for u in users:
        try:
            compute_employee_evaluation(u.id, period_type, year, month, created_by_id=created_by_id)
            count += 1
        except Exception:
            db.session.rollback()
            continue
    return count


def _normalize_indicator_code(value: Any, fallback: str = "") -> str:
    raw = str(value or fallback or "").strip()
    raw = raw.replace("\u200f", "").replace("\u200e", "").replace("\ufeff", "")
    raw = re.sub(r"[\s\-/]+", "_", raw, flags=re.UNICODE)
    raw = re.sub(r"[^\w:]+", "", raw, flags=re.UNICODE)
    return raw.strip("_").lower()[:100]


def _resolve_import_user(row: dict[str, Any]) -> User | None:
    user_id = to_int(pick(row, "user_id", "userid", "معرف المستخدم", "رقم المستخدم"))
    if user_id:
        user = User.query.get(user_id)
        if user:
            return user

    email = to_str(pick(row, "email", "البريد", "البريد الإلكتروني", "ايميل"))
    if email:
        user = User.query.filter(func.lower(User.email) == email.strip().lower()).first()
        if user:
            return user

    employee_no_raw = pick(row, "employee_no", "employee number", "رقم الموظف", "الرقم الوظيفي")
    employee_no = to_str(employee_no_raw)
    if employee_no and employee_no.endswith(".0"):
        employee_no = employee_no[:-2]
    if employee_no:
        emp_file = EmployeeFile.query.filter(EmployeeFile.employee_no == employee_no.strip()).first()
        if emp_file:
            return User.query.get(emp_file.user_id)

    name = to_str(pick(row, "employee", "employee_name", "name", "الموظف", "اسم الموظف"))
    if name:
        name = name.strip()
        emp_file = EmployeeFile.query.filter(EmployeeFile.full_name_quad == name).first()
        if emp_file:
            return User.query.get(emp_file.user_id)
        return User.query.filter(User.name == name).first()

    return None


def _row_period(row: dict[str, Any], defaults: dict[str, Any] | None = None) -> tuple[str, int, int | None]:
    defaults = defaults or {}
    ptype_raw = to_str(pick(row, "period_type", "period", "نوع الفترة", "الفترة")) or defaults.get("period_type") or "MONTHLY"
    ptype = _period_type(str(ptype_raw))
    year = to_int(pick(row, "year", "السنة"), default=to_int(defaults.get("year"), default=datetime.utcnow().year))
    if not year:
        raise ValueError("السنة مطلوبة")
    month = to_int(pick(row, "month", "الشهر"), default=to_int(defaults.get("month"), default=None))
    if ptype == "MONTHLY" and not month:
        raise ValueError("الشهر مطلوب للتقييم الشهري")
    return ptype, int(year), int(month) if ptype == "MONTHLY" and month else None


def _import_score(row: dict[str, Any]) -> float | None:
    score_5 = _parse_float(pick(row, "score_5", "score5", "علامة من 5", "العلامة من 5", "العلامة"))
    if score_5 is not None:
        return clamp(score_5, 0.0, 5.0) * 20.0

    score_100 = _parse_float(pick(row, "score_100", "score100", "علامة من 100", "العلامة من 100"))
    if score_100 is not None:
        return clamp(score_100, 0.0, 100.0)
    return None


def _import_detail(
    row: dict[str, Any],
    *,
    row_number: int,
    score_100: float | None,
    imported_by_id: int | None,
    filename: str,
) -> dict[str, Any]:
    explanation = to_str(pick(row, "explanation", "reason", "comment", "تفسير", "تفسير العلامة", "سبب العلامة", "ملاحظات"))
    evidence = to_str(pick(row, "evidence", "reference", "دليل", "مرجع", "الدليل أو المرجع"))
    source = to_str(pick(row, "source", "المصدر")) or filename
    reference_date = pick(row, "reference_date", "date", "تاريخ المرجع", "التاريخ")

    return {
        "row": row_number,
        "imported_at": datetime.utcnow().isoformat(timespec="seconds"),
        "imported_by_id": imported_by_id,
        "source": source or "",
        "score_5": score_5_from_100(score_100) if score_100 is not None else None,
        "score_100": round(score_100, 2) if score_100 is not None else None,
        "explanation": explanation or "",
        "evidence": evidence or "",
        "reference_date": _date_text(reference_date),
    }


def _count_imported_indicators(components: dict[str, dict[str, Any]]) -> int:
    count = 0
    for c in components.values():
        if c.get("source") == "imported" or c.get("imported_override") or c.get("imported_details"):
            count += 1
    return count


def import_indicator_evaluations(
    file_storage,
    *,
    imported_by_id: int | None = None,
    defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Import per-indicator scores/explanations from Excel.

    Supported row scope:
      - one indicator for one employee
      - all indicators for one employee
      - one/all indicators for many employees
    """
    sheet, rows, _headers = read_excel_rows(file_storage, max_rows=50000)
    filename = getattr(file_storage, "filename", "") or ""
    stats: dict[str, Any] = {
        "sheet": sheet,
        "rows": len(rows),
        "applied": 0,
        "errors": 0,
        "error_samples": [],
        "runs": 0,
    }
    touched_runs: dict[int, EmployeeEvaluationRun] = {}
    touched_counts: dict[int, int] = {}

    for row_number, row in enumerate(rows, start=2):
        try:
            user = _resolve_import_user(row)
            if not user:
                raise ValueError("لم يتم العثور على الموظف")

            period_type, year, month = _row_period(row, defaults)
            start, end = _period_range(period_type, year, month)
            run = _get_or_create_run(user, period_type, year, month, created_by_id=imported_by_id)
            breakdown = _load_breakdown(run, user, start, end)
            components = breakdown.setdefault("components", {})
            metrics = breakdown.setdefault("metrics", {})

            indicator_code = to_str(pick(row, "indicator_code", "indicator", "code", "كود المؤشر", "رمز المؤشر", "المؤشر"))
            indicator_label = to_str(pick(row, "indicator_label", "label", "اسم المؤشر", "عنوان المؤشر"))
            key = _normalize_indicator_code(indicator_code, indicator_label or f"imported_{row_number}")
            if not key:
                raise ValueError("كود المؤشر أو اسم المؤشر مطلوب")

            score_100 = _import_score(row)
            weight_raw = _parse_float(pick(row, "weight", "الوزن", "وزن المؤشر"))
            explanation = to_str(pick(row, "explanation", "reason", "comment", "تفسير", "تفسير العلامة", "سبب العلامة", "ملاحظات"))
            detail = _import_detail(
                row,
                row_number=row_number,
                score_100=score_100,
                imported_by_id=imported_by_id,
                filename=filename,
            )

            component = components.get(key)
            if component:
                component.setdefault("imported_details", []).append(detail)
                if indicator_label:
                    component["label"] = indicator_label
                if explanation:
                    component["explanation"] = explanation
                if weight_raw is not None:
                    component["weight"] = clamp(weight_raw, 0.0, 100.0)
                if score_100 is not None:
                    if not component.get("imported_override"):
                        component["system_norm"] = component.get("norm")
                        component["system_score_5"] = component.get("score_5")
                        component["system_indicator_score_100"] = component.get("indicator_score_100")
                    component["norm"] = clamp(score_100 / 100.0, 0.0, 1.0)
                    component["source"] = "imported"
                    component["imported_override"] = True
                    component["value"] = {
                        "العلامة المستوردة من 5": score_5_from_100(score_100),
                        "العلامة المستوردة من 100": round(score_100, 2),
                        "تفسير": explanation or "",
                    }
            else:
                effective_score = score_100 if score_100 is not None else 0.0
                effective_weight = clamp(weight_raw if weight_raw is not None else (10.0 if score_100 is not None else 0.0), 0.0, 100.0)
                components[key] = _component(
                    label=indicator_label or indicator_code or key,
                    description="مؤشر مستورد من ملف تقييم.",
                    weight=effective_weight,
                    value={
                        "العلامة المستوردة من 5": score_5_from_100(effective_score) if score_100 is not None else "",
                        "العلامة المستوردة من 100": round(effective_score, 2) if score_100 is not None else "",
                        "تفسير": explanation or "",
                    },
                    norm=clamp(effective_score / 100.0, 0.0, 1.0),
                    explanation=explanation or "تم استيراد تفاصيل هذا المؤشر من ملف تقييم.",
                    details=[detail],
                    source="imported",
                )

            metrics["imported_indicators"] = _count_imported_indicators(components)
            score_100_new, score_5_new = _finalize_components(components)
            breakdown["score"] = {"score_100": score_100_new, "score_5": score_5_new}
            run.score_100 = score_100_new
            run.score_5 = score_5_new
            run.summary = _build_summary(metrics)
            run.breakdown_json = json.dumps(breakdown, ensure_ascii=False)
            run.created_by_id = imported_by_id
            run.created_at = datetime.utcnow()

            touched_runs[run.id] = run
            touched_counts[run.id] = touched_counts.get(run.id, 0) + 1
            stats["applied"] += 1
        except Exception as exc:
            stats["errors"] += 1
            if len(stats["error_samples"]) < 10:
                stats["error_samples"].append(f"صف {row_number}: {exc}")

    db.session.flush()
    for run_id, run in touched_runs.items():
        db.session.add(AuditLog(
            user_id=imported_by_id,
            action="EVALUATION_INDICATORS_IMPORT",
            note=f"sheet={sheet} rows={touched_counts.get(run_id, 0)} file={filename}",
            target_type="EMPLOYEE_EVALUATION_RUN",
            target_id=run.id,
            created_at=datetime.utcnow(),
        ))

    db.session.commit()
    stats["runs"] = len(touched_runs)
    return stats
