from __future__ import annotations

from datetime import date, datetime, timedelta

from extensions import db
from models import (
    EmployeeFile,
    HRAttendanceSchedulePlan,
    Notification,
    Role,
    User,
)
from services.hr_request_workflow import (
    resolve_responsible_managers,
    secretary_general_user_ids,
)
from services.notification_email import (
    ATTENDANCE_SCHEDULE_EMAIL_MODE,
    enqueue_notification_email,
)
from utils.notification_links import safe_local_notification_url
from utils.role_codes import canonical_role_key


ATTENDANCE_SCHEDULE_STATUSES = {
    "DRAFT",
    "SUBMITTED",
    "MANAGER_APPROVED",
    "FINAL_APPROVED",
}
ATTENDANCE_SCHEDULE_DAY_TYPES = {"WORK", "REMOTE", "OFF"}
_SUPER_ADMIN_ROLE_KEYS = {
    "SUPERADMIN",
    "SUPERADMINISTRATOR",
    "SYSTEMADMIN",
    "ADMINISTRATOR",
    "ROOT",
    "SUPERUSER",
    "SYSADMIN",
    "ADMINROOT",
}


def _super_admin_role_labels() -> set[str]:
    labels = set()
    try:
        for role in Role.query.all():
            if canonical_role_key(role.code) not in _SUPER_ADMIN_ROLE_KEYS:
                continue
            labels.update({
                (role.code or "").strip().casefold(),
                (role.name_ar or "").strip().casefold(),
                (role.name_en or "").strip().casefold(),
            })
    except Exception:
        pass
    labels.discard("")
    return labels


def _is_super_admin_account(user: User, role_labels: set[str]) -> bool:
    raw_role = (getattr(user, "role", None) or "").strip()
    role_key = canonical_role_key(raw_role)
    if role_key in _SUPER_ADMIN_ROLE_KEYS:
        return True
    if "سوبر" in raw_role and ("أدمن" in raw_role or "ادمن" in raw_role):
        return True
    return raw_role.casefold() in role_labels


def attendance_schedule_final_approver_user_ids() -> list[int]:
    user_ids = set(secretary_general_user_ids())
    role_labels = _super_admin_role_labels()
    for user in User.query.all():
        if _is_super_admin_account(user, role_labels):
            user_ids.add(int(user.id))
    return sorted(user_ids)


def attendance_schedule_stakeholder_user_ids(
    employee: User,
    manager: User | None = None,
    final_approver_user_ids: list[int] | None = None,
) -> list[int]:
    """Return the employee, every responsible manager, and final approvers."""
    if not employee:
        return []
    selected_managers = resolve_responsible_managers(int(employee.id))
    if not selected_managers and manager:
        selected_managers = [manager]
    final_user_ids = (
        attendance_schedule_final_approver_user_ids()
        if final_approver_user_ids is None
        else final_approver_user_ids
    )
    user_ids = {int(employee.id), *final_user_ids}
    user_ids.update(
        int(selected_manager.id)
        for selected_manager in selected_managers
        if selected_manager and selected_manager.id
    )
    return sorted(user_ids)


def notify_attendance_schedule_stakeholders(
    employee: User,
    message: str,
    *,
    level: str = "INFO",
    link_url: str | None = None,
    manager: User | None = None,
    event_key: str | None = None,
    final_approver_user_ids: list[int] | None = None,
) -> int:
    safe_message = (message or "يوجد تحديث جديد على جدول الدوام.")[:255]
    safe_link = safe_local_notification_url(link_url)
    safe_event_key = (event_key or "")[:64] or None
    created = 0
    for user_id in attendance_schedule_stakeholder_user_ids(
        employee,
        manager,
        final_approver_user_ids,
    ):
        if safe_event_key and Notification.query.filter_by(
            user_id=user_id,
            event_key=safe_event_key,
        ).first():
            continue
        notification = Notification(
            user_id=user_id,
            message=safe_message,
            type=(level or "INFO").strip().upper(),
            source="portal",
            link_url=safe_link,
            event_key=safe_event_key,
            is_read=False,
            is_mirror=False,
            email_delivery_mode=ATTENDANCE_SCHEDULE_EMAIL_MODE,
        )
        db.session.add(notification)
        db.session.flush()
        enqueue_notification_email(notification)
        created += 1
    return created


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
    final_approver_user_ids = attendance_schedule_final_approver_user_ids()
    for employee_file in EmployeeFile.query.all():
        user = employee_file.user
        if not user or not attendance_schedule_needs_reminder(
            plans.get(user.id),
            period_start,
            today,
        ):
            continue
        event_key = f"att-schedule-reminder-{period_start_text}-{user.id}"[:64]
        exists = Notification.query.filter_by(
            user_id=user.id,
            event_key=event_key,
        ).first()
        if exists:
            continue
        notify_attendance_schedule_stakeholders(
            user,
            f"تذكير بجدول دوام {user.full_name}: يرجى استكمال جدول الأسبوعين قبل بداية الأسبوع الثاني.",
            level="WARNING",
            link_url=(
                "/portal/hr/attendance/work-schedule"
                f"?start={period_start_text}&employee_id={user.id}"
            ),
            event_key=event_key,
            final_approver_user_ids=final_approver_user_ids,
        )
        sent += 1
    return sent
