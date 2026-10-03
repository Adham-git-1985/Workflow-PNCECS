"""Shared parsing helpers for a user's private calendar events.

The database stores event timestamps as naive UTC values, while browser forms
are entered in the application's configured local timezone.  Keeping that
conversion here makes both the Portal calendar and the Workflow shortcut use
the same validation rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone

from utils.timezone import app_timezone, to_local_time


CALENDAR_EVENT_TYPE_LABELS = {
    "PERSONAL": "موعد شخصي",
    "TASK": "مهمة",
    "CONFERENCE": "مؤتمر",
    "REVIEW": "مراجعة",
    "WORKFLOW": "مسار",
}

USER_EDITABLE_CALENDAR_EVENT_TYPES = (
    "PERSONAL",
    "TASK",
    "CONFERENCE",
    "REVIEW",
)

# A reminder is a property of the user's private calendar entry.  Meetings
# retain their own reminder policy in the meetings module.
NO_CALENDAR_REMINDER_VALUE = "NONE"
DEFAULT_CALENDAR_REMINDER_MINUTES = 24 * 60
CALENDAR_REMINDER_OPTIONS = (
    (NO_CALENDAR_REMINDER_VALUE, "بدون تذكير"),
    ("60", "قبل ساعة"),
    (str(DEFAULT_CALENDAR_REMINDER_MINUTES), "قبل يوم"),
    ("10080", "قبل أسبوع"),
)
_ALLOWED_CALENDAR_REMINDER_MINUTES = {
    int(value)
    for value, _label in CALENDAR_REMINDER_OPTIONS
    if value != NO_CALENDAR_REMINDER_VALUE
}


@dataclass(frozen=True)
class CalendarEventInput:
    title: str
    event_type: str
    description: str | None
    all_day: bool
    start_at: datetime
    end_at: datetime | None
    reminder_minutes_before: int | None


def _text(value) -> str:
    return str(value or "").strip()


def _checked(value) -> bool:
    return _text(value).lower() in {"1", "true", "yes", "on"}


def _parse_date(value) -> date | None:
    try:
        return datetime.strptime(_text(value), "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_time(value) -> time | None:
    try:
        return datetime.strptime(_text(value), "%H:%M").time()
    except ValueError:
        return None


def local_datetime_to_utc(value: datetime) -> datetime:
    """Convert a local, naive browser datetime into naive UTC for storage."""
    timezone_value = app_timezone()
    if hasattr(timezone_value, "localize"):
        local_value = timezone_value.localize(value)
    else:
        local_value = value.replace(tzinfo=timezone_value)
    return local_value.astimezone(timezone.utc).replace(tzinfo=None)


def _parse_reminder_minutes(value) -> tuple[int | None, str | None]:
    """Return a supported lead time or a user-facing validation error."""
    raw_value = _text(value)
    if not raw_value:
        return DEFAULT_CALENDAR_REMINDER_MINUTES, None
    if raw_value.upper() == NO_CALENDAR_REMINDER_VALUE:
        return None, None
    try:
        minutes = int(raw_value)
    except (TypeError, ValueError):
        return None, "خيار التذكير غير صالح."
    if minutes not in _ALLOWED_CALENDAR_REMINDER_MINUTES:
        return None, "خيار التذكير غير صالح."
    return minutes, None


def parse_calendar_event_input(values, *, forced_event_type: str | None = None):
    """Validate browser form values and return ``(input, errors)``.

    ``values`` can be any form-like object that supports ``.get`` (such as
    Flask's ``ImmutableMultiDict``).  The caller controls whether an event
    type is selected by the user or fixed by a source such as a workflow.
    """
    title = _text(values.get("title"))
    event_type = (forced_event_type or _text(values.get("event_type")) or "PERSONAL").upper()
    event_day = _parse_date(values.get("event_date"))
    all_day = _checked(values.get("all_day"))
    start_time = _parse_time(values.get("start_time"))
    end_time_raw = _text(values.get("end_time"))
    end_time = _parse_time(end_time_raw) if end_time_raw else None
    reminder_minutes_before, reminder_error = _parse_reminder_minutes(
        values.get("reminder_minutes_before"),
    )

    errors: list[str] = []
    if not title:
        errors.append("عنوان الموعد مطلوب.")
    if event_type not in CALENDAR_EVENT_TYPE_LABELS:
        errors.append("نوع الموعد غير صالح.")
    if not event_day:
        errors.append("تاريخ الموعد مطلوب.")
    if not all_day and not start_time:
        errors.append("وقت البداية مطلوب للموعد المحدد بوقت.")
    if end_time_raw and not end_time:
        errors.append("وقت النهاية غير صالح.")
    if reminder_error:
        errors.append(reminder_error)
    if errors:
        return None, errors

    start_local = datetime.combine(event_day, time.min if all_day else start_time)
    end_local = datetime.combine(event_day, end_time) if end_time and not all_day else None
    if end_local and end_local <= start_local:
        return None, ["وقت النهاية يجب أن يكون بعد وقت البداية."]

    return CalendarEventInput(
        title=title[:255],
        event_type=event_type,
        description=_text(values.get("description")) or None,
        all_day=all_day,
        start_at=local_datetime_to_utc(start_local),
        end_at=local_datetime_to_utc(end_local) if end_local else None,
        reminder_minutes_before=reminder_minutes_before,
    ), []


def calendar_event_form_values(event=None, *, default_date: date | None = None) -> dict:
    """Build stable values for the shared create/edit event form."""
    if event is not None:
        start_local = to_local_time(getattr(event, "start_at", None))
        end_local = to_local_time(getattr(event, "end_at", None))
        all_day = bool(getattr(event, "all_day", False))
        reminder_minutes = getattr(
            event,
            "reminder_minutes_before",
            DEFAULT_CALENDAR_REMINDER_MINUTES,
        )
        return {
            "title": getattr(event, "title", "") or "",
            "event_type": getattr(event, "event_type", "PERSONAL") or "PERSONAL",
            "event_date": start_local.strftime("%Y-%m-%d") if start_local else "",
            "start_time": "" if all_day or not start_local else start_local.strftime("%H:%M"),
            "end_time": "" if all_day or not end_local else end_local.strftime("%H:%M"),
            "all_day": all_day,
            "description": getattr(event, "description", "") or "",
            "reminder_minutes_before": (
                NO_CALENDAR_REMINDER_VALUE
                if reminder_minutes is None
                else str(int(reminder_minutes))
            ),
        }

    day = default_date or datetime.now(app_timezone()).date()
    return {
        "title": "",
        "event_type": "PERSONAL",
        "event_date": day.isoformat(),
        "start_time": "09:00",
        "end_time": "",
        "all_day": True,
        "description": "",
        "reminder_minutes_before": str(DEFAULT_CALENDAR_REMINDER_MINUTES),
    }


def submitted_calendar_event_form_values(values, *, fallback: dict | None = None) -> dict:
    """Preserve submitted fields when validation returns the form to a user."""
    result = dict(fallback or calendar_event_form_values())
    for name in (
        "title",
        "event_type",
        "event_date",
        "start_time",
        "end_time",
        "description",
        "reminder_minutes_before",
    ):
        if name in values:
            result[name] = _text(values.get(name))
    result["all_day"] = _checked(values.get("all_day"))
    return result
