"""Weekly employee roster reports built from the effective attendance schedule.

This module intentionally lives beside the existing Masar reports instead of
changing the current Diwan report.  The report is a read-only weekly view of
the published schedule.  A schedule plan becomes effective on its
``period_start`` date and keeps recurring until a later published plan
replaces it; ``period_end`` is only the stored seven-day preview boundary.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta

from models import (
    Department,
    Directorate,
    Division,
    EmployeeFile,
    HRAttendanceSchedulePlan,
    HRLookupItem,
    OrgNode,
    OrgNodeAssignment,
    Section,
    TransportDriver,
    User,
)


WORKDAY_LABELS = {
    6: "الأحد",
    0: "الاثنين",
    1: "الثلاثاء",
    2: "الأربعاء",
    3: "الخميس",
}

REPORT_GROUPS = (
    ("office", "مكتب الأمين العام"),
    ("finance", "الشؤون المالية والإدارية والتخطيط"),
    ("drivers", "السائقين"),
    ("messengers", "المراسلين"),
    ("projects", "المشاريع"),
    ("publications", "المطبوعات"),
    ("organizations", "المنظمات والتخصصية"),
    ("relations", "العلاقات العامة والإعلام"),
    ("legal", "الشؤون القانونية"),
    ("social", "التنوع الاجتماعي"),
    ("advisor_48", "مستشار 48"),
)

OTHER_GROUP = ("other", "جهات أخرى / غير مصنفة")

LEADERSHIP_ROLE_CODES = frozenset(
    {
        "DIRECTORATE_HEAD",
        "GENERAL_DIRECTOR",
        "GENERAL_ADMINISTRATION",
        "GENERAL_DIRECTORATE",
        "ASSISTANT_SECRETARY_GENERAL",
        "ASSISTANT_TO_SECRETARY_GENERAL",
        "SECRETARY_GENERAL_ASSISTANT",
    }
)

PUBLISHED_SCHEDULE_STATUSES = ("ADMIN_APPROVED", "FINAL_APPROVED")

FINANCE_CONTEXT_TOKENS = (
    "الشؤون الماليه",
    "الشؤون الاداريه",
    "الاداره العامه للشؤون الاداريه والماليه",
    "موارد وشؤون ماليه",
    "الموارد والشؤون الماليه",
    "موارد بشريه",
    "الموارد البشريه",
    "تخطيط",
)

REPORT_GROUP_NAME_OVERRIDES = (
    (frozenset(("دعاء", "سلامه")), "office"),
    (frozenset(("نور", "برغوثي")), "organizations"),
    (frozenset(("بيان", "كحله")), "organizations"),
    (frozenset(("رحيق", "شتيه")), "organizations"),
    (frozenset(("شوكت", "بركات")), "organizations"),
)

_ARABIC_DIGIT_TRANSLATION = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩", "0123456789"
)


def _clean_value(value) -> str:
    """Return a useful string and ignore import placeholders such as ``nan``."""

    text = str(value or "").strip()
    if text.casefold() in {"nan", "none", "null"}:
        return ""
    return text


def _normalise_text(value) -> str:
    text = _clean_value(value)
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = text.translate(_ARABIC_DIGIT_TRANSLATION)
    text = (
        text.replace("أ", "ا")
        .replace("إ", "ا")
        .replace("آ", "ا")
        .replace("ى", "ي")
        .replace("ة", "ه")
        .replace("ـ", "")
    )
    return re.sub(r"\s+", " ", text).strip().casefold()


def _short_employee_name(value) -> str:
    """Keep the compact first/family-name style used by the reference sheet."""

    full_name = _clean_value(value)
    if not full_name:
        return "بدون اسم"
    cleaned = re.sub(r'["“”«»()]', " ", full_name)
    parts = [part for part in cleaned.split() if part]
    if len(parts) <= 2:
        return " ".join(parts)
    return f"{parts[0]} {parts[-1]}"


def _sunday_on_or_before(value: date) -> date:
    # Python weekday(): Monday=0 ... Sunday=6.
    return value - timedelta(days=(value.weekday() + 1) % 7)


def parse_week_start(raw_value: str | None = None, *, today: date | None = None) -> date:
    """Parse a selected date and normalize it to the Sunday of its workweek."""

    selected = None
    if raw_value:
        try:
            selected = date.fromisoformat(str(raw_value).strip())
        except (TypeError, ValueError):
            selected = None
    return _sunday_on_or_before(selected or today or date.today())


def _classify_group_from_context(
    *,
    department: str = "",
    directorate: str = "",
    section: str = "",
    division: str = "",
    job_title: str = "",
    admin_title: str = "",
    org_context: tuple[str, ...] = (),
    finance_context: tuple[str, ...] | None = None,
) -> str:
    """Map the employee-file placement/title to the report's visual sections."""

    structural = _normalise_text(
        " ".join(
            part
            for part in (department, directorate, section, division, *org_context)
            if _clean_value(part)
        )
    )
    finance_structural = _normalise_text(
        " ".join(
            part
            for part in (
                department,
                directorate,
                section,
                division,
                *(finance_context if finance_context is not None else org_context),
            )
            if _clean_value(part)
        )
    )
    titles = _normalise_text(
        " ".join(
            value
            for value in (_clean_value(job_title), _clean_value(admin_title))
            if value
        )
    )
    combined = " ".join(part for part in (structural, titles) if part)

    if "سائق" in titles or "سواق" in titles:
        return "drivers"
    if "مراسل" in titles or "مراسله" in titles:
        return "messengers"
    if "مستشار" in combined and "48" in combined:
        return "advisor_48"
    if any(token in finance_structural for token in FINANCE_CONTEXT_TOKENS):
        return "finance"
    if any(
        token in combined
        for token in (
            "نوع اجتماعي",
            "النوع الاجتماعي",
            "تنوع اجتماعي",
            "تطوير وادماج",
        )
    ):
        return "social"
    if any(token in combined for token in ("قانون", "استشار")):
        return "legal"
    # Structural placement wins over a broad job title.  Khaloud's title
    # mentions projects, but her current primary directorate is organizations;
    # Adham's project branch, on the other hand, is present in the hierarchy.
    if any(token in structural for token in ("مشاريع", "مشروع", "برامج")):
        return "projects"
    # A specialized directorate is a stronger placement signal than a job
    # title such as "information systems" inside that directorate.
    if any(token in combined for token in ("منظمات", "متخصص", "الكسو", "اليونسكو", "الايسيسكو")):
        return "organizations"
    if any(token in combined for token in ("مطبوعات", "معلوماتيه", "معلومات")):
        return "publications"
    if any(token in combined for token in ("علاقات", "اعلام", "اتصالات")):
        return "relations"
    if any(token in structural for token in ("مكتب الامين العام", "الامانه العامه")):
        return "office"
    if any(token in titles for token in ("امين عام", "سكرتير", "سكرتيره")):
        return "office"
    return "other"


def _employee_name(employee_file: EmployeeFile | None, user) -> str:
    if employee_file:
        name = _clean_value(employee_file.full_name_quad)
        if name:
            return name
    return _clean_value(getattr(user, "full_name", None)) or f"الموظف #{getattr(user, 'id', '')}"


def _report_group_name_override(
    employee_file: EmployeeFile | None,
    user,
) -> str | None:
    """Apply confirmed current placement for names with ambiguous legacy titles."""

    name_tokens = set(
        _normalise_text(_employee_name(employee_file, user)).split()
    )
    for required_tokens, group_key in REPORT_GROUP_NAME_OVERRIDES:
        if required_tokens.issubset(name_tokens):
            return group_key
    return None


def _lookup_labels(employee_files: list[EmployeeFile]) -> dict[int, str]:
    lookup_ids = {
        lookup_id
        for employee_file in employee_files
        for lookup_id in (
            employee_file.job_title_lookup_id,
            employee_file.admin_title_lookup_id,
        )
        if lookup_id
    }
    if not lookup_ids:
        return {}
    return {
        int(item.id): _clean_value(item.label)
        for item in HRLookupItem.query.filter(HRLookupItem.id.in_(lookup_ids)).all()
    }


def _dynamic_org_contexts(users: list[User]) -> dict[int, tuple[str, ...]]:
    """Return each user's current dynamic-node names and their ancestors."""

    user_ids = [int(user.id) for user in users if getattr(user, "id", None)]
    contexts: dict[int, list[str]] = {user_id: [] for user_id in user_ids}
    if not user_ids:
        return {}

    assignments = (
        OrgNodeAssignment.query
        .filter(OrgNodeAssignment.user_id.in_(user_ids))
        .order_by(
            OrgNodeAssignment.user_id.asc(),
            OrgNodeAssignment.is_primary.desc(),
            OrgNodeAssignment.id.asc(),
        )
        .all()
    )
    assigned_user_ids = set()

    def add_node_chain(user_id: int, node) -> None:
        seen_node_ids = set()
        while node is not None:
            node_id = getattr(node, "id", None)
            if node_id in seen_node_ids:
                break
            if node_id is not None:
                seen_node_ids.add(node_id)
            label = _clean_value(getattr(node, "name_ar", ""))
            if label and label not in contexts[user_id]:
                contexts[user_id].append(label)
            node = getattr(node, "parent", None)

    for assignment in assignments:
        user_id = int(assignment.user_id)
        # The primary assignment is the current placement.  A user may also
        # retain historical/secondary assignments that must not override it.
        if user_id in assigned_user_ids:
            continue
        assigned_user_ids.add(user_id)
        assignment_title = _clean_value(getattr(assignment, "title", ""))
        if assignment_title and assignment_title not in contexts[user_id]:
            contexts[user_id].append(assignment_title)
        add_node_chain(user_id, getattr(assignment, "node", None))

    # Older records may keep only users.org_node_id without an assignment row.
    # Include that node as a fallback so the report still follows the current
    # hierarchy instead of an orphaned EmployeeFile foreign key.
    fallback_node_ids = {
        int(user.org_node_id)
        for user in users
        if getattr(user, "id", None)
        and int(user.id) not in assigned_user_ids
        and getattr(user, "org_node_id", None)
    }
    if fallback_node_ids:
        fallback_nodes = (
            OrgNode.query
            .filter(OrgNode.id.in_(fallback_node_ids))
            .all()
        )
        nodes_by_id = {int(node.id): node for node in fallback_nodes}
        for user in users:
            if not getattr(user, "id", None) or int(user.id) in assigned_user_ids:
                continue
            node = nodes_by_id.get(int(getattr(user, "org_node_id", 0) or 0))
            if node is not None:
                add_node_chain(int(user.id), node)

    return {user_id: tuple(labels) for user_id, labels in contexts.items()}


def _user_legacy_contexts(users: list[User]) -> dict[int, tuple[str, ...]]:
    """Resolve the current fixed-table placement stored directly on users."""

    directorate_ids = {
        int(user.directorate_id)
        for user in users
        if getattr(user, "directorate_id", None)
    }
    department_ids = {
        int(user.department_id)
        for user in users
        if getattr(user, "department_id", None)
    }
    section_ids = {
        int(user.section_id)
        for user in users
        if getattr(user, "section_id", None)
    }
    division_ids = {
        int(user.division_id)
        for user in users
        if getattr(user, "division_id", None)
    }
    directorates = {
        int(item.id): _clean_value(item.name_ar)
        for item in Directorate.query.filter(Directorate.id.in_(directorate_ids)).all()
    } if directorate_ids else {}
    departments = {
        int(item.id): _clean_value(item.name_ar)
        for item in Department.query.filter(Department.id.in_(department_ids)).all()
    } if department_ids else {}
    sections = {
        int(item.id): _clean_value(item.name_ar)
        for item in Section.query.filter(Section.id.in_(section_ids)).all()
    } if section_ids else {}
    divisions = {
        int(item.id): _clean_value(item.name_ar)
        for item in Division.query.filter(Division.id.in_(division_ids)).all()
    } if division_ids else {}

    result: dict[int, tuple[str, ...]] = {}
    for user in users:
        if not getattr(user, "id", None):
            continue
        labels = []
        for lookup, value in (
            (departments, getattr(user, "department_id", None)),
            (directorates, getattr(user, "directorate_id", None)),
            (sections, getattr(user, "section_id", None)),
            (divisions, getattr(user, "division_id", None)),
        ):
            label = lookup.get(int(value)) if value else ""
            if label and label not in labels:
                labels.append(label)
        result[int(user.id)] = tuple(labels)
    return result


def _transport_driver_user_ids(
    users: list[User],
    employee_files: list[EmployeeFile],
) -> set[int]:
    """Match active transport drivers even when the driver row lacks user_id."""

    active_drivers = TransportDriver.query.filter(TransportDriver.status == "ACTIVE").all()
    if not active_drivers:
        return set()
    employee_file_map = {int(item.user_id): item for item in employee_files}
    driver_user_ids = {
        int(row.user_id)
        for row in active_drivers
        if getattr(row, "user_id", None)
    }
    for user in users:
        employee_file = employee_file_map.get(int(user.id)) if getattr(user, "id", None) else None
        employee_tokens = set(
            _normalise_text(_employee_name(employee_file, user)).split()
        )
        if not employee_tokens:
            continue
        for driver in active_drivers:
            driver_tokens = set(_normalise_text(getattr(driver, "name", "")).split())
            if driver_tokens and driver_tokens.issubset(employee_tokens):
                driver_user_ids.add(int(user.id))
                break
    return driver_user_ids


def _is_finance_context(value: str) -> bool:
    normalized = _normalise_text(value)
    return any(token in normalized for token in FINANCE_CONTEXT_TOKENS)


def _employee_group(
    employee_file: EmployeeFile | None,
    lookup_labels: dict[int, str],
    *,
    user: User | None = None,
    org_context: tuple[str, ...] = (),
    force_driver: bool = False,
    finance_context: tuple[str, ...] | None = None,
) -> str:
    # The context is the current primary node followed by its ancestors.  A
    # specific current node can therefore take precedence over a broad parent
    # branch (for example, organizations over the assistant's projects branch).
    effective_org_context = org_context
    current_user = user or getattr(employee_file, "user", None)
    if not employee_file and not current_user:
        return "other"
    if force_driver:
        return "drivers"
    name_override = _report_group_name_override(employee_file, current_user)
    if name_override:
        return name_override
    department = getattr(getattr(employee_file, "department", None), "name_ar", "")
    directorate = getattr(getattr(employee_file, "directorate", None), "name_ar", "")
    section = getattr(getattr(employee_file, "section", None), "name_ar", "")
    division = getattr(getattr(employee_file, "division", None), "name_ar", "")
    job_title_lookup_id = getattr(employee_file, "job_title_lookup_id", None)
    job_title = lookup_labels.get(job_title_lookup_id, "")
    user_job_title = getattr(current_user, "job_title", "")
    if user_job_title and user_job_title not in job_title:
        job_title = " ".join(part for part in (job_title, user_job_title) if part)
    admin_title_lookup_id = getattr(employee_file, "admin_title_lookup_id", None)
    admin_title = lookup_labels.get(admin_title_lookup_id, "")
    return _classify_group_from_context(
        department=department,
        directorate=directorate,
        section=section,
        division=division,
        job_title=job_title,
        admin_title=admin_title,
        org_context=effective_org_context,
        finance_context=(
            (department, directorate, section, division)
            + tuple(finance_context or ())
        ),
    )


def _leadership_user_ids(
    users: list[User],
    employee_files: list[EmployeeFile],
    lookup_labels: dict[int, str],
) -> set[int]:
    """Return general-director and assistant-secretary users for the footer rows."""

    employee_file_map = {int(item.user_id): item for item in employee_files}
    leadership_ids = set()
    for user in users:
        user_id = getattr(user, "id", None)
        if not user_id:
            continue
        role = _clean_value(getattr(user, "role", "")).upper()
        employee_file = employee_file_map.get(int(user_id))
        title_values = [_clean_value(getattr(user, "job_title", ""))]
        if employee_file:
            title_values.extend(
                (
                    _clean_value(lookup_labels.get(employee_file.job_title_lookup_id, "")),
                    _clean_value(lookup_labels.get(employee_file.admin_title_lookup_id, "")),
                )
            )
        combined_title = _normalise_text(" ".join(title_values))
        if (
            role in LEADERSHIP_ROLE_CODES
            or "مدير عام" in combined_title
            or "مساعد الامين العام" in combined_title
        ):
            leadership_ids.add(int(user_id))
    return leadership_ids


def _leadership_priority_user_ids(
    users: list[User],
    employee_files: list[EmployeeFile],
) -> dict[int, int]:
    """Return the requested leading positions for the leadership footer."""

    employee_file_map = {int(item.user_id): item for item in employee_files}
    priority_tokens = (
        (0, frozenset(("رائف", "سلامه"))),
        (1, frozenset(("خلود", "حنتش"))),
    )
    priority_by_user_id = {}
    for user in users:
        user_id = getattr(user, "id", None)
        if not user_id:
            continue
        employee_file = employee_file_map.get(int(user_id))
        name_tokens = set(
            _normalise_text(_employee_name(employee_file, user)).split()
        )
        for priority, required_tokens in priority_tokens:
            if required_tokens.issubset(name_tokens):
                priority_by_user_id[int(user_id)] = priority
                break
    return priority_by_user_id


def _schedule_day_for_date(plan, work_day: date):
    """Resolve a recurring plan's stored weekday template for ``work_day``."""

    if not plan:
        return None
    exact_key = work_day.isoformat()
    weekday = work_day.weekday()
    weekday_row = None
    for row in getattr(plan, "days", None) or []:
        if _clean_value(getattr(row, "work_date", "")) == exact_key:
            return row
        try:
            row_day = date.fromisoformat(_clean_value(getattr(row, "work_date", "")))
        except (TypeError, ValueError):
            continue
        if row_day.weekday() == weekday and weekday_row is None:
            weekday_row = row
    return weekday_row


def _effective_schedule_days(users: list[User], days: list[date]) -> dict[str, dict[int, object]]:
    """Return the effective published schedule row for each user and report day."""

    if not users or not days:
        return {day.isoformat(): {} for day in days}

    user_ids = [int(user.id) for user in users if getattr(user, "id", None)]
    if not user_ids:
        return {day.isoformat(): {} for day in days}

    period_end = max(days).isoformat()
    plans = (
        HRAttendanceSchedulePlan.query
        .filter(HRAttendanceSchedulePlan.user_id.in_(user_ids))
        .filter(HRAttendanceSchedulePlan.period_start <= period_end)
        .filter(HRAttendanceSchedulePlan.status.in_(PUBLISHED_SCHEDULE_STATUSES))
        .order_by(
            HRAttendanceSchedulePlan.user_id.asc(),
            HRAttendanceSchedulePlan.period_start.desc(),
            HRAttendanceSchedulePlan.version_no.desc(),
            HRAttendanceSchedulePlan.id.desc(),
        )
        .all()
    )
    plans_by_user: dict[int, list[HRAttendanceSchedulePlan]] = {}
    for plan in plans:
        plans_by_user.setdefault(int(plan.user_id), []).append(plan)

    result = {day.isoformat(): {} for day in days}
    for user_id in user_ids:
        user_plans = plans_by_user.get(user_id, [])
        for work_day in days:
            day_key = work_day.isoformat()
            effective_plan = next(
                (
                    plan
                    for plan in user_plans
                    if _clean_value(getattr(plan, "period_start", "")) <= day_key
                ),
                None,
            )
            schedule_day = _schedule_day_for_date(effective_plan, work_day)
            if not schedule_day:
                continue
            # The report is the in-office weekly roster.  Remote and off days
            # must not be treated as attendance, otherwise every user appears
            # on every weekday and the leadership rows stop varying by day.
            if _clean_value(getattr(schedule_day, "day_type", "")).upper() != "WORK":
                continue
            result[day_key][user_id] = schedule_day
    return result


def build_actual_attendance_report(
    week_start: str | None = None,
    *,
    today: date | None = None,
) -> dict:
    """Build the Sunday-to-Thursday matrix from the effective work schedule."""

    period_start = parse_week_start(week_start, today=today)
    period_end = period_start + timedelta(days=4)
    days = [period_start + timedelta(days=offset) for offset in range(5)]
    day_keys = [item.isoformat() for item in days]

    all_employee_files = EmployeeFile.query.all()
    all_users = User.query.all()
    employee_file_map = {int(item.user_id): item for item in all_employee_files}
    lookup_labels = _lookup_labels(all_employee_files)
    user_legacy_contexts = _user_legacy_contexts(all_users)
    dynamic_org_contexts = _dynamic_org_contexts(all_users)
    transport_driver_user_ids = _transport_driver_user_ids(
        all_users,
        all_employee_files,
    )
    schedule_days = _effective_schedule_days(all_users, days)
    leadership_user_ids = _leadership_user_ids(
        all_users,
        all_employee_files,
        lookup_labels,
    )
    leadership_priority_by_user_id = _leadership_priority_user_ids(
        all_users,
        all_employee_files,
    )

    all_group_keys = [key for key, _label in REPORT_GROUPS]
    member_maps = {
        group_key: {day_key: {} for day_key in day_keys}
        for group_key in all_group_keys
    }
    member_maps[OTHER_GROUP[0]] = {day_key: {} for day_key in day_keys}

    user_by_id = {
        int(user.id): user
        for user in all_users
        if getattr(user, "id", None)
    }
    for day_key in day_keys:
        for user_id in schedule_days[day_key]:
            employee_file = employee_file_map.get(user_id)
            user = user_by_id.get(user_id)
            dynamic_context = dynamic_org_contexts.get(user_id, ())
            legacy_context = user_legacy_contexts.get(user_id, ())
            legacy_finance_context = tuple(
                label for label in legacy_context if _is_finance_context(label)
            )
            if dynamic_context:
                group_context = dynamic_context + legacy_finance_context
                group_finance_context = dynamic_context[:2] + legacy_finance_context
            else:
                group_context = legacy_context
                group_finance_context = legacy_context
            full_name = _employee_name(employee_file, user)
            entry = {
                "user_id": user_id,
                "name": _short_employee_name(full_name),
                "full_name": full_name,
                "status": _clean_value(
                    getattr(schedule_days[day_key][user_id], "day_type", "")
                ).upper() or "WORK",
            }
            group_key = _employee_group(
                employee_file,
                lookup_labels,
                user=user,
                org_context=group_context,
                finance_context=group_finance_context,
                force_driver=user_id in transport_driver_user_ids,
            )
            member_maps.setdefault(group_key, {key: {} for key in day_keys})
            member_maps[group_key][day_key][user_id] = entry

    labels = dict(REPORT_GROUPS)
    if any(member_maps[OTHER_GROUP[0]][key] for key in day_keys):
        labels[OTHER_GROUP[0]] = OTHER_GROUP[1]

    groups = []
    ordered_group_keys = [key for key, _label in REPORT_GROUPS]
    if OTHER_GROUP[0] in labels:
        ordered_group_keys.append(OTHER_GROUP[0])
    for group_key in ordered_group_keys:
        group_days = []
        for day, day_key in zip(days, day_keys):
            entries = sorted(
                member_maps[group_key][day_key].values(),
                key=lambda item: (item["name"], item["full_name"], item["user_id"]),
            )
            group_days.append(
                {
                    "date": day,
                    "key": day_key,
                    "label": WORKDAY_LABELS.get(day.weekday(), day.strftime("%A")),
                    "names": [item["name"] for item in entries],
                    "entries": entries,
                    "count": len(entries),
                }
            )
        row_count = max((item["count"] for item in group_days), default=0) or 1
        matrix_rows = [
            {
                "names": [
                    day["names"][row_index]
                    if row_index < len(day["names"])
                    else ""
                    for day in group_days
                ]
            }
            for row_index in range(row_count)
        ]
        groups.append(
            {
                "key": group_key,
                "label": labels[group_key],
                "days": group_days,
                "rows": matrix_rows,
                "row_count": row_count,
                "total": sum(item["count"] for item in group_days),
            }
        )

    ordered_day_people = {}
    for day_key in day_keys:
        ordered_entries = []
        for group_key in ordered_group_keys:
            ordered_entries.extend(
                sorted(
                    member_maps[group_key][day_key].values(),
                    key=lambda item: (item["name"], item["full_name"], item["user_id"]),
                )
            )
        ordered_day_people[day_key] = ordered_entries

    # Preserve the existing leadership order for all other directors, while
    # keeping Raef first and Khaloud immediately after him when she is on the
    # schedule for that day.
    leadership_base_rank = {}
    for day_key in day_keys:
        for entry in ordered_day_people[day_key]:
            user_id = entry["user_id"]
            if (
                user_id in leadership_user_ids
                and user_id not in leadership_base_rank
            ):
                leadership_base_rank[user_id] = len(leadership_base_rank)

    def leadership_sort_key(entry):
        user_id = entry["user_id"]
        priority = leadership_priority_by_user_id.get(user_id)
        return (
            0 if priority is not None else 1,
            priority if priority is not None else 0,
            leadership_base_rank.get(user_id, len(leadership_base_rank)),
        )

    leadership_day_people = {
        day_key: sorted(
            (
                entry
                for entry in ordered_day_people[day_key]
                if entry["user_id"] in leadership_user_ids
            ),
            key=leadership_sort_key,
        )
        for day_key in day_keys
    }
    max_leadership_rows = max(
        (len(items) for items in leadership_day_people.values()),
        default=0,
    )
    leadership_rows = [
        {
            "number": index + 1,
            "names": [
                leadership_day_people[day_key][index]["name"]
                if index < len(leadership_day_people[day_key])
                else ""
                for day_key in day_keys
            ],
        }
        for index in range(max_leadership_rows)
    ]
    total_counts = [len(ordered_day_people[day_key]) for day_key in day_keys]

    return {
        "period_start": period_start,
        "period_end": period_end,
        "days": [
            {
                "date": day,
                "key": day.isoformat(),
                "label": WORKDAY_LABELS.get(day.weekday(), day.strftime("%A")),
            }
            for day in days
        ],
        "groups": groups,
        "total_counts": total_counts,
        "leadership_rows": leadership_rows,
        "total_attendance": sum(total_counts),
        "has_data": any(schedule_days[day_key] for day_key in day_keys),
    }
