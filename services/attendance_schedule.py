from __future__ import annotations

from datetime import date, datetime, timedelta

from extensions import db
from models import (
    EmployeeFile,
    HRAttendanceSchedulePlan,
    Notification,
)


ATTENDANCE_SCHEDULE_STATUSES = {
    "DRAFT",
    "SUBMITTED",
    "MANAGER_APPROVED",
    "FINAL_APPROVED",
}
ATTENDANCE_SCHEDULE_DAY_TYPES = {"WORK", "REMOTE", "OFF"}


def attendance_schedule_cycle_start(reference_day: date | None = None) -> date:
    selected_day = reference_day or date.today()
    anchor = date(2020, 1, 5)
    cycle_number = (selected_day - anchor).days // 14
    return anchor + timedelta(days=cycle_number * 14)


def normalize_attendance_schedule_cycle(
    value: str | date | None,
    reference_day: date | None = None,
) -> date:
    if isinstance(value, date):
        selected_day = value
    else:
        try:
            selected_day = datetime.strptime(str(value or ""), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            selected_day = reference_day or date.today()
    return attendance_schedule_cycle_start(selected_day)


def attendance_schedule_cycle_days(period_start: date) -> list[date]:
    return [period_start + timedelta(days=offset) for offset in range(14)]


def attendance_schedule_needs_reminder(
    plan: HRAttendanceSchedulePlan | None,
    period_start: date,
    reference_day: date | None = None,
) -> bool:
    today = reference_day or date.today()
    second_week_start = period_start + timedelta(days=7)
    days_until_second_week = (second_week_start - today).days
    if days_until_second_week < 0 or days_until_second_week > 3:
        return False
    if plan is None or plan.status == "DRAFT":
        return True
    return len(plan.days) < 14


def send_attendance_schedule_reminders(reference_day: date | None = None) -> int:
    today = reference_day or date.today()
    period_start = attendance_schedule_cycle_start(today)
    if not attendance_schedule_needs_reminder(None, period_start, today):
        return 0

    period_start_text = period_start.isoformat()
    plans = {}
    rows = (
        HRAttendanceSchedulePlan.query
        .filter_by(period_start=period_start_text)
        .order_by(
            HRAttendanceSchedulePlan.user_id.asc(),
            HRAttendanceSchedulePlan.version_no.desc(),
        )
        .all()
    )
    for row in rows:
        plans.setdefault(row.user_id, row)
    sent = 0
    for employee_file in EmployeeFile.query.all():
        user = employee_file.user
        if not user or not attendance_schedule_needs_reminder(
            plans.get(user.id),
            period_start,
            today,
        ):
            continue
        event_key = f"att-schedule-{period_start_text}-{user.id}"[:64]
        exists = Notification.query.filter_by(
            user_id=user.id,
            event_key=event_key,
        ).first()
        if exists:
            continue
        db.session.add(Notification(
            user_id=user.id,
            message="تذكير: أكمل جدول دوام الأسبوعين وأرسله لمديرك قبل بداية الأسبوع الثاني.",
            type="WARNING",
            source="portal",
            link_url=f"/portal/hr/attendance/work-schedule?start={period_start_text}",
            event_key=event_key,
            is_read=False,
            is_mirror=False,
        ))
        sent += 1
    return sent
