from datetime import date, datetime, time, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app, has_app_context
import pytz


DEFAULT_APP_TIMEZONE = "Asia/Jerusalem"


def app_timezone_name() -> str:
    if not has_app_context():
        return DEFAULT_APP_TIMEZONE
    return current_app.config.get("APP_TIMEZONE", DEFAULT_APP_TIMEZONE)


def app_timezone() -> tzinfo:
    try:
        return ZoneInfo(app_timezone_name())
    except ZoneInfoNotFoundError:
        try:
            return pytz.timezone(app_timezone_name())
        except pytz.UnknownTimeZoneError:
            return pytz.timezone(DEFAULT_APP_TIMEZONE)


def to_local_time(value: datetime | None) -> datetime | None:
    """Convert UTC timestamps from the database to the configured local time."""
    if value is None:
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    return value.astimezone(app_timezone())


def format_local_datetime(value: datetime | None, pattern: str = "%Y-%m-%d %H:%M") -> str:
    local_value = to_local_time(value)
    return local_value.strftime(pattern) if local_value else ""


def local_day_start_utc(value: date) -> datetime:
    """Return a local calendar day's start as a naive UTC database value."""
    local_start = datetime.combine(value, time.min)
    local_timezone = app_timezone()
    if hasattr(local_timezone, "localize"):
        local_start = local_timezone.localize(local_start)
    else:
        local_start = local_start.replace(tzinfo=local_timezone)
    return local_start.astimezone(timezone.utc).replace(tzinfo=None)
