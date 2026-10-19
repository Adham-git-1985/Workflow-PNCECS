"""Read models for open HR approval requests shown in Masar reports."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.orm import noload

from models import (
    HRAttendanceSchedulePlan,
    HRAttendanceSpecialCase,
    HRLeaveRequest,
    HRPermissionRequest,
    HRRequestApprovalStep,
)


OPEN_APPROVAL_REPORT_MAX_ROWS = 2000

OPEN_APPROVAL_CATEGORIES = (
    ("LEAVE", "الإجازات"),
    ("PERMISSION", "المغادرات"),
    ("MATERNITY_DEPARTURE", "مغادرات الأمومة"),
    ("SCHEDULE_CHANGE", "طلبات تغيير جدول الدوام"),
    ("DAILY_ATTENDANCE", "تعديلات الدوام اليومية"),
)

OPEN_APPROVAL_CATEGORY_LABELS = dict(OPEN_APPROVAL_CATEGORIES)

OPEN_SCHEDULE_STATUSES = {
    "DRAFT",
    "SUBMITTED",
    "MANAGER_APPROVED",
    "GENERAL_DIRECTOR_APPROVED",
}

OPEN_APPROVAL_STAGE_LABELS = {
    "DRAFT": "بانتظار الإرسال",
    "DIRECT_MANAGER": "المدير المباشر",
    "GENERAL_DIRECTOR": "المدير العام",
    "ADMINISTRATIVE_AFFAIRS": "الشؤون الإدارية",
    "HR": "الشؤون البشرية",
    "SECRETARY_GENERAL": "الأمين العام",
    "MANAGER": "المدير المباشر",
    "FINAL": "الاعتماد النهائي",
}

OPEN_APPROVAL_STATUS_LABELS = {
    "DRAFT": "مسودة مفتوحة",
    "SUBMITTED": "بانتظار الاعتماد",
    "PENDING": "بانتظار الاعتماد",
    "MANAGER_APPROVED": "بانتظار الاعتماد التالي",
    "GENERAL_DIRECTOR_APPROVED": "بانتظار الاعتماد النهائي",
}


def _chunks(values: set[int], size: int = 400):
    ordered = sorted({int(value) for value in values if value})
    for offset in range(0, len(ordered), size):
        yield ordered[offset:offset + size]


def _name(user: Any, fallback_id: Any) -> str:
    return (
        getattr(user, "full_name", None)
        or getattr(user, "name", None)
        or getattr(user, "email", None)
        or f"المستخدم #{fallback_id}"
    )


def _text(value: Any) -> str:
    return str(value or "").strip().casefold()


def _status_label(status: str) -> str:
    normalized = (status or "").strip().upper()
    return OPEN_APPROVAL_STATUS_LABELS.get(normalized, normalized or "غير محدد")


def _stage_label(stage: str | None) -> str:
    normalized = (stage or "").strip().upper()
    return OPEN_APPROVAL_STAGE_LABELS.get(normalized, normalized or "قيد المراجعة")


def _submitted_at(row: Any) -> datetime | None:
    return (
        getattr(row, "submitted_at", None)
        or getattr(row, "created_at", None)
        or getattr(row, "updated_at", None)
    )


def _row(
    *,
    kind: str,
    request_id: int,
    employee_id: int | None,
    employee_name: str,
    status: str,
    stage: str,
    submitted_at: datetime | None,
    period: str,
    details: str,
    note: str = "",
    day: str | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
) -> dict[str, Any]:
    normalized_status = (status or "").strip().upper()
    return {
        "kind": kind,
        "kind_label": OPEN_APPROVAL_CATEGORY_LABELS[kind],
        "request_id": int(request_id),
        "employee_id": int(employee_id) if employee_id else None,
        "employee": employee_name,
        "status": normalized_status,
        "status_label": _status_label(normalized_status),
        "stage": _stage_label(stage),
        "stage_code": (stage or "").strip().upper(),
        "submitted_at": submitted_at,
        "period": period or "—",
        "details": details or "—",
        "note": note or "",
        "day": day,
        "period_start": period_start,
        "period_end": period_end,
    }


def _pending_hr_steps(
    leave_ids: set[int],
    permission_ids: set[int],
) -> dict[tuple[str, int], HRRequestApprovalStep]:
    """Load the current pending step for all leave/permission rows in batches."""
    result: dict[tuple[str, int], HRRequestApprovalStep] = {}
    for kind, request_ids in (("LEAVE", leave_ids), ("PERMISSION", permission_ids)):
        for request_id_chunk in _chunks(request_ids):
            steps = (
                HRRequestApprovalStep.query
                .filter(
                    HRRequestApprovalStep.request_kind == kind,
                    HRRequestApprovalStep.request_id.in_(request_id_chunk),
                    HRRequestApprovalStep.status == "PENDING",
                )
                .order_by(
                    HRRequestApprovalStep.request_id.asc(),
                    HRRequestApprovalStep.step_order.asc(),
                    HRRequestApprovalStep.id.asc(),
                )
                .all()
            )
            for step in steps:
                result.setdefault((kind, int(step.request_id)), step)
    return result


def _load_rows(category: str) -> tuple[list[dict[str, Any]], dict[str, bool]]:
    """Read each indexed approval source once and normalize it for the UI/email."""
    requested = {category} if category in OPEN_APPROVAL_CATEGORY_LABELS else {
        value for value, _label in OPEN_APPROVAL_CATEGORIES
    }
    rows: list[dict[str, Any]] = []
    truncated: dict[str, bool] = {value: False for value, _label in OPEN_APPROVAL_CATEGORIES}

    leave_rows = []
    if "LEAVE" in requested:
        leave_rows = (
            HRLeaveRequest.query
            .options(noload(HRLeaveRequest.attachments))
            .filter(HRLeaveRequest.status.in_(("SUBMITTED", "PENDING")))
            .order_by(HRLeaveRequest.id.desc())
            .limit(OPEN_APPROVAL_REPORT_MAX_ROWS + 1)
            .all()
        )
        truncated["LEAVE"] = len(leave_rows) > OPEN_APPROVAL_REPORT_MAX_ROWS

    permission_rows = []
    if "PERMISSION" in requested:
        permission_rows = (
            HRPermissionRequest.query
            .filter(HRPermissionRequest.status.in_(("SUBMITTED", "PENDING")))
            .order_by(HRPermissionRequest.id.desc())
            .limit(OPEN_APPROVAL_REPORT_MAX_ROWS + 1)
            .all()
        )
        truncated["PERMISSION"] = len(permission_rows) > OPEN_APPROVAL_REPORT_MAX_ROWS

    schedule_rows = []
    if "SCHEDULE_CHANGE" in requested:
        schedule_rows = (
            HRAttendanceSchedulePlan.query
            .filter(
                HRAttendanceSchedulePlan.request_type == "CHANGE_REQUEST",
                HRAttendanceSchedulePlan.status.in_(sorted(OPEN_SCHEDULE_STATUSES)),
            )
            .order_by(HRAttendanceSchedulePlan.id.desc())
            .limit(OPEN_APPROVAL_REPORT_MAX_ROWS + 1)
            .all()
        )
        truncated["SCHEDULE_CHANGE"] = len(schedule_rows) > OPEN_APPROVAL_REPORT_MAX_ROWS

    maternity_rows = []
    if "MATERNITY_DEPARTURE" in requested:
        maternity_rows = (
            HRAttendanceSpecialCase.query
            .filter(
                HRAttendanceSpecialCase.kind == "MATERNITY_DEPARTURE",
                or_(
                    HRAttendanceSpecialCase.approval_status == "PENDING",
                    and_(
                        HRAttendanceSpecialCase.approval_status.is_(None),
                        HRAttendanceSpecialCase.applied.is_(False),
                    ),
                ),
            )
            .order_by(HRAttendanceSpecialCase.id.desc())
            .limit(OPEN_APPROVAL_REPORT_MAX_ROWS + 1)
            .all()
        )
        truncated["MATERNITY_DEPARTURE"] = len(maternity_rows) > OPEN_APPROVAL_REPORT_MAX_ROWS

    manual_rows = []
    if "DAILY_ATTENDANCE" in requested:
        manual_rows = (
            HRAttendanceSpecialCase.query
            .filter(
                HRAttendanceSpecialCase.kind == "MANUAL_ATTENDANCE",
                or_(
                    HRAttendanceSpecialCase.approval_status == "PENDING",
                    and_(
                        HRAttendanceSpecialCase.approval_status.is_(None),
                        HRAttendanceSpecialCase.applied.is_(False),
                    ),
                ),
            )
            .order_by(HRAttendanceSpecialCase.id.desc())
            .limit(OPEN_APPROVAL_REPORT_MAX_ROWS + 1)
            .all()
        )
        truncated["DAILY_ATTENDANCE"] = len(manual_rows) > OPEN_APPROVAL_REPORT_MAX_ROWS

    leave_rows = leave_rows[:OPEN_APPROVAL_REPORT_MAX_ROWS]
    permission_rows = permission_rows[:OPEN_APPROVAL_REPORT_MAX_ROWS]
    schedule_rows = schedule_rows[:OPEN_APPROVAL_REPORT_MAX_ROWS]
    maternity_rows = maternity_rows[:OPEN_APPROVAL_REPORT_MAX_ROWS]
    manual_rows = manual_rows[:OPEN_APPROVAL_REPORT_MAX_ROWS]

    pending_steps = _pending_hr_steps(
        {int(row.id) for row in leave_rows if row.id},
        {int(row.id) for row in permission_rows if row.id},
    )

    for request_row in leave_rows:
        step = pending_steps.get(("LEAVE", int(request_row.id)))
        stage = getattr(step, "stage_code", None) or "DIRECT_MANAGER"
        leave_type = getattr(getattr(request_row, "leave_type", None), "name_ar", None) or "إجازة"
        rows.append(_row(
            kind="LEAVE",
            request_id=request_row.id,
            employee_id=request_row.user_id,
            employee_name=_name(request_row.user, request_row.user_id),
            status=request_row.status,
            stage=stage,
            submitted_at=_submitted_at(request_row),
            period=f"{request_row.start_date} → {request_row.end_date}",
            details=leave_type,
            note=request_row.note or "",
        ))

    for request_row in permission_rows:
        step = pending_steps.get(("PERMISSION", int(request_row.id)))
        stage = getattr(step, "stage_code", None) or "DIRECT_MANAGER"
        permission_type = getattr(getattr(request_row, "permission_type", None), "name_ar", None) or "مغادرة"
        time_range = " → ".join(
            value for value in (request_row.from_time, request_row.to_time) if value
        )
        details = permission_type + (f" ({time_range})" if time_range else "")
        rows.append(_row(
            kind="PERMISSION",
            request_id=request_row.id,
            employee_id=request_row.user_id,
            employee_name=_name(request_row.user, request_row.user_id),
            status=request_row.status,
            stage=stage,
            submitted_at=_submitted_at(request_row),
            period=request_row.day,
            details=details,
            note=request_row.note or "",
            day=request_row.day,
        ))

    for request_row in maternity_rows:
        status = (request_row.approval_status or "PENDING").upper()
        rows.append(_row(
            kind="MATERNITY_DEPARTURE",
            request_id=request_row.id,
            employee_id=request_row.user_id,
            employee_name=_name(request_row.user, request_row.user_id),
            status=status,
            stage="ADMINISTRATIVE_AFFAIRS",
            submitted_at=_submitted_at(request_row),
            period=f"{request_row.day} → {request_row.day_to or request_row.day}",
            details="مغادرة أمومة",
            note=request_row.note or "",
            day=request_row.day,
        ))

    for plan in schedule_rows:
        status = (plan.status or "DRAFT").upper()
        stage = {
            "DRAFT": "DRAFT",
            "SUBMITTED": "MANAGER",
            "MANAGER_APPROVED": "GENERAL_DIRECTOR" if plan.general_director_user_id else "FINAL",
            "GENERAL_DIRECTOR_APPROVED": "FINAL",
        }.get(status, status)
        rows.append(_row(
            kind="SCHEDULE_CHANGE",
            request_id=plan.id,
            employee_id=plan.user_id,
            employee_name=_name(plan.user, plan.user_id),
            status=status,
            stage=stage,
            submitted_at=_submitted_at(plan),
            period=f"{plan.period_start} → {plan.period_end}",
            details="طلب تغيير جدول الدوام",
            note=plan.employee_note or plan.manager_note or "",
            period_start=plan.period_start,
            period_end=plan.period_end,
        ))

    for special_case in manual_rows:
        status = (special_case.approval_status or "PENDING").upper()
        stage = "SECRETARY_GENERAL" if special_case.approved_by_id or special_case.approved_at else "ADMINISTRATIVE_AFFAIRS"
        rows.append(_row(
            kind="DAILY_ATTENDANCE",
            request_id=special_case.id,
            employee_id=special_case.user_id,
            employee_name=_name(special_case.user, special_case.user_id),
            status=status,
            stage=stage,
            submitted_at=_submitted_at(special_case),
            period=f"{special_case.day} → {special_case.day_to or special_case.day}",
            details=f"{special_case.start_time or '—'} → {special_case.end_time or '—'}",
            note=special_case.note or "",
            day=special_case.day,
        ))

    return rows, truncated


def _sort_timestamp(value: Any) -> float:
    try:
        return value.timestamp() if value else 0.0
    except (AttributeError, TypeError, ValueError, OSError):
        return 0.0


def build_open_approval_report(
    viewer: Any | None = None,
    *,
    search: str = "",
    category: str = "",
    page: int = 1,
    per_page: int | None = 50,
) -> dict[str, Any]:
    """Build the open HR approvals report without per-row database queries.

    ``viewer`` is accepted for the same report-builder contract as the employee
    report. Access is enforced by the Masar report permission at the route and
    by the configured report viewer for email delivery.
    """
    normalized_category = (category or "").strip().upper()
    if normalized_category not in OPEN_APPROVAL_CATEGORY_LABELS:
        normalized_category = ""
    search_text = _text(search)
    rows, truncated = _load_rows(normalized_category)

    if search_text:
        rows = [
            row
            for row in rows
            if search_text in _text(" ".join(
                str(row.get(key) or "")
                for key in ("employee", "kind_label", "status_label", "stage", "period", "details", "note")
            ))
        ]

    rows.sort(
        key=lambda row: (
            -_sort_timestamp(row.get("submitted_at")),
            -int(row.get("request_id") or 0),
        )
    )
    counts = Counter(row["kind"] for row in rows)

    try:
        page = max(1, int(page or 1))
    except (TypeError, ValueError):
        page = 1
    if per_page is None:
        page_rows = rows
        pages = 1
        page = 1
    else:
        try:
            per_page = max(10, min(int(per_page or 50), 100))
        except (TypeError, ValueError):
            per_page = 50
        pages = max(1, (len(rows) + per_page - 1) // per_page)
        page = min(page, pages)
        page_rows = rows[(page - 1) * per_page:page * per_page]

    return {
        "rows": page_rows,
        "all_rows": rows,
        "category_options": OPEN_APPROVAL_CATEGORIES,
        "category": normalized_category,
        "category_label": OPEN_APPROVAL_CATEGORY_LABELS.get(normalized_category, "كل الأنواع"),
        "total_rows": len(rows),
        "counts": {value: int(counts.get(value, 0)) for value, _label in OPEN_APPROVAL_CATEGORIES},
        "total_open": len(rows),
        "pages": pages,
        "page": page,
        "truncated": truncated,
        "truncated_any": any(truncated.values()),
        "max_rows_per_category": OPEN_APPROVAL_REPORT_MAX_ROWS,
    }
