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
    hr_notification_user_ids,
    resolve_responsible_managers,
    resolve_general_director,
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
    "GENERAL_DIRECTOR_APPROVED",
    "ADMIN_APPROVED",
    "FINAL_APPROVED",
    "REJECTED",
    "CANCELLED",
}
ATTENDANCE_SCHEDULE_DAY_TYPES = {"WORK", "REMOTE", "OFF"}
# Each plan stores one seven-day pattern.  It repeats from the plan's
# effective-from date until a later published plan replaces it.
ATTENDANCE_SCHEDULE_PERIOD_DAYS = 7
ATTENDANCE_SCHEDULE_PUBLISHED_STATUSES = {"ADMIN_APPROVED", "FINAL_APPROVED"}
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
    # The secretary general is the named final approver for user-facing
    # workflow purposes. Super administrators remain an internal fallback
    # approver, but their role must never be exposed in user-facing text.
    user_ids: set[int] = set(secretary_general_user_ids())
    role_labels = _super_admin_role_labels()
    for user in User.query.all():
        if _is_super_admin_account(user, role_labels) or canonical_role_key(getattr(user, "role", None)) == "ADMIN":
            user_ids.add(int(user.id))
    return sorted(user_ids)


def attendance_schedule_stakeholder_user_ids(
    employee: User,
    manager: User | None = None,
    final_approver_user_ids: list[int] | None = None,
    *,
    include_general_director: bool = False,
    include_hr: bool = False,
) -> list[int]:
    """Return the users who need to know about a schedule event.

    General directors and HR are opt-in recipients because ordinary schedule
    notifications historically went only to the employee, direct managers,
    and the final approver.  Change requests explicitly enable both groups.
    """
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
    if include_general_director:
        general_director = resolve_general_director(int(employee.id))
        if general_director and general_director.id:
            user_ids.add(int(general_director.id))
    if include_hr:
        user_ids.update(int(user_id) for user_id in hr_notification_user_ids() if user_id)
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
    include_general_director: bool = False,
    include_hr: bool = False,
) -> int:
    safe_message = (message or "يوجد تحديث جديد على جدول الدوام.")[:255]
    safe_link = safe_local_notification_url(link_url)
    safe_event_key = (event_key or "")[:64] or None
    created = 0
    for user_id in attendance_schedule_stakeholder_user_ids(
        employee,
        manager,
        final_approver_user_ids,
        include_general_director=include_general_director,
        include_hr=include_hr,
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
    cycle_number = (selected_day - anchor).days // ATTENDANCE_SCHEDULE_PERIOD_DAYS
    return anchor + timedelta(days=cycle_number * ATTENDANCE_SCHEDULE_PERIOD_DAYS)


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
    return [
        period_start + timedelta(days=offset)
        for offset in range(ATTENDANCE_SCHEDULE_PERIOD_DAYS)
    ]


def attendance_schedule_needs_reminder(
    plan: HRAttendanceSchedulePlan | None,
    period_start: date,
    reference_day: date | None = None,
) -> bool:
    # Employees no longer complete weekly schedules.  HR owns the schedule,
    # while employees submit change requests, so the old "complete the second
    # week" reminder would be misleading and is intentionally disabled.
    return False


def send_attendance_schedule_reminders(reference_day: date | None = None) -> int:
    # Kept as a no-op for the scheduled job so existing deployments do not
    # send stale employee-completion reminders after the workflow change.
    return 0
