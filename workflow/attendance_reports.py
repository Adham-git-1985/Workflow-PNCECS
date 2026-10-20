"""Reports built from the actual daily attendance summaries.

This module intentionally lives beside the existing Masar reports instead of
changing the current Diwan report.  The report is a read-only weekly view of
employees who have an actual attendance record for each working day.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta

from models import AttendanceDailySummary, EmployeeFile, HRLookupItem


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
) -> str:
    """Map the employee-file placement/title to the report's visual sections."""

    structural = _normalise_text(
        " ".join(
            part
            for part in (department, directorate, section, division)
            if _clean_value(part)
        )
    )
    titles = _normalise_text(" ".join((job_title, admin_title)))
    combined = " ".join(part for part in (structural, titles) if part)

    if "سائق" in titles or "سواق" in titles:
        return "drivers"
    if "مراسل" in titles or "مراسله" in titles:
        return "messengers"
    if "مستشار" in combined and "48" in combined:
        return "advisor_48"
    if any(token in combined for token in ("نوع اجتماعي", "تنوع اجتماعي", "تطوير وادماج")):
        return "social"
    if any(token in combined for token in ("قانون", "استشار")):
        return "legal"
    if any(token in combined for token in ("مشاريع", "مشروع", "برامج")):
        return "projects"
    if any(token in combined for token in ("مطبوعات", "معلوماتيه", "معلومات")):
        return "publications"
    if any(token in combined for token in ("منظمات", "متخصص", "الكسو", "اليونسكو", "الايسيسكو")):
        return "organizations"
    if any(token in combined for token in ("علاقات", "اعلام", "اتصالات")):
        return "relations"
    if any(token in combined for token in ("شؤون ماليه", "موارد وشؤون ماليه", "شؤون اداريه", "تخطيط", "موارد بشريه")):
        return "finance"
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


def _employee_group(employee_file: EmployeeFile | None, lookup_labels: dict[int, str]) -> str:
    if not employee_file:
        return "other"
    department = getattr(getattr(employee_file, "department", None), "name_ar", "")
    directorate = getattr(getattr(employee_file, "directorate", None), "name_ar", "")
    section = getattr(getattr(employee_file, "section", None), "name_ar", "")
    division = getattr(getattr(employee_file, "division", None), "name_ar", "")
    job_title = lookup_labels.get(employee_file.job_title_lookup_id, "")
    if not job_title:
        job_title = getattr(getattr(employee_file, "user", None), "job_title", "")
    admin_title = lookup_labels.get(employee_file.admin_title_lookup_id, "")
    return _classify_group_from_context(
        department=department,
        directorate=directorate,
        section=section,
        division=division,
        job_title=job_title,
        admin_title=admin_title,
    )


def _has_actual_attendance(row: AttendanceDailySummary) -> bool:
    status = _clean_value(row.status).upper()
    if status in {"ABSENT", "WEEKLY_OFF", "OFF", "HOLIDAY"}:
        return False
    return bool(
        row.first_in
        or row.last_out
        or (row.work_minutes or 0) > 0
    )


def build_actual_attendance_report(
    week_start: str | None = None,
    *,
    today: date | None = None,
) -> dict:
    """Build the Sunday-to-Thursday actual attendance matrix."""

    period_start = parse_week_start(week_start, today=today)
    period_end = period_start + timedelta(days=4)
    days = [period_start + timedelta(days=offset) for offset in range(5)]
    day_keys = [item.isoformat() for item in days]

    rows = (
        AttendanceDailySummary.query
        .filter(AttendanceDailySummary.day >= period_start.isoformat())
        .filter(AttendanceDailySummary.day <= period_end.isoformat())
        .order_by(AttendanceDailySummary.day.asc(), AttendanceDailySummary.user_id.asc())
        .all()
    )
    rows = [row for row in rows if _has_actual_attendance(row)]
    user_ids = {int(row.user_id) for row in rows if row.user_id is not None}

    employee_files = []
    if user_ids:
        employee_files = (
            EmployeeFile.query
            .filter(EmployeeFile.user_id.in_(user_ids))
            .all()
        )
    employee_file_map = {int(item.user_id): item for item in employee_files}
    lookup_labels = _lookup_labels(employee_files)

    all_group_keys = [key for key, _label in REPORT_GROUPS]
    member_maps = {
        group_key: {day_key: {} for day_key in day_keys}
        for group_key in all_group_keys
    }
    member_maps[OTHER_GROUP[0]] = {day_key: {} for day_key in day_keys}

    for row in rows:
        day_key = _clean_value(row.day)
        if day_key not in day_keys:
            continue
        user_id = int(row.user_id)
        employee_file = employee_file_map.get(user_id)
        user = getattr(row, "user", None)
        full_name = _employee_name(employee_file, user)
        entry = {
            "user_id": user_id,
            "name": _short_employee_name(full_name),
            "full_name": full_name,
            "status": _clean_value(row.status).upper() or "OK",
        }
        group_key = _employee_group(employee_file, lookup_labels)
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
        groups.append(
            {
                "key": group_key,
                "label": labels[group_key],
                "days": group_days,
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

    max_total_rows = max((len(items) for items in ordered_day_people.values()), default=0)
    grand_total_rows = [
        {
            "number": index + 1,
            "names": [
                ordered_day_people[day_key][index]["name"]
                if index < len(ordered_day_people[day_key])
                else ""
                for day_key in day_keys
            ],
        }
        for index in range(max_total_rows)
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
        "grand_total_rows": grand_total_rows,
        "total_attendance": sum(total_counts),
        "has_data": bool(rows),
    }
