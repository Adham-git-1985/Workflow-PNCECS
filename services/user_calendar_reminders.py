"""Reminder emission for private user-calendar events.

The sender is deliberately separate from the calendar routes: it is invoked by
the background worker, writes an in-app ``Notification``, and queues the
existing durable notification-email outbox.  This keeps a browser visit from
being a prerequisite for a reminder.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from sqlalchemy import or_

from extensions import db
from models import Notification, UserCalendarEvent
from services.notification_email import (
    CALENDAR_REMINDER_EMAIL_MODE,
    enqueue_notification_email,
)
from services.user_calendar import (
    CALENDAR_EVENT_TYPE_LABELS,
    local_datetime_to_utc,
)
from utils.timezone import to_local_time


MAX_REMINDER_LEAD_MINUTES = 7 * 24 * 60
ALL_DAY_REMINDER_HOUR = 9


def calendar_event_reminder_at(event: UserCalendarEvent) -> datetime | None:
    """Return the UTC timestamp at which an event should issue its reminder.

    For an all-day event, midnight is not a useful notification time.  Its
    reminder is therefore sent at 09:00 local time after applying the chosen
    lead (so the default one-day reminder arrives at 09:00 the previous day).
    """
    start_at = getattr(event, "start_at", None)
    try:
        lead_minutes = int(getattr(event, "reminder_minutes_before", 0) or 0)
    except (TypeError, ValueError):
        return None
    if not start_at or lead_minutes <= 0 or lead_minutes > MAX_REMINDER_LEAD_MINUTES:
        return None

    if not bool(getattr(event, "all_day", False)):
        return start_at - timedelta(minutes=lead_minutes)

    local_start = to_local_time(start_at)
    if local_start is None:
        return None
    reminder_local = datetime.combine(
        local_start.date(),
        time(hour=ALL_DAY_REMINDER_HOUR),
    ) - timedelta(minutes=lead_minutes)
    return local_datetime_to_utc(reminder_local)


def _event_date_label(event: UserCalendarEvent) -> str:
    local_start = to_local_time(getattr(event, "start_at", None))
    if local_start is None:
        return "في الموعد المسجل"
    if bool(getattr(event, "all_day", False)):
        return f"بتاريخ {local_start.strftime('%Y-%m-%d')} طوال اليوم"
    return f"في {local_start.strftime('%Y-%m-%d')} الساعة {local_start.strftime('%H:%M')}"


def _event_calendar_url(event: UserCalendarEvent) -> str:
    local_start = to_local_time(getattr(event, "start_at", None))
    if local_start is None:
        return "/portal/calendar"
    return f"/portal/calendar?date={local_start.date().isoformat()}"


def _event_reminder_message(event: UserCalendarEvent) -> str:
    event_type = (getattr(event, "event_type", None) or "PERSONAL").upper()
    event_kind = CALENDAR_EVENT_TYPE_LABELS.get(event_type, "موعد")
    title = (getattr(event, "title", None) or "موعدك").strip()
    return f"تذكير {event_kind}: {title} — {_event_date_label(event)}."[:255]


def _claim_event_reminder(event: UserCalendarEvent, now: datetime) -> bool:
    """Atomically reserve a reminder so parallel worker processes do not duplicate it."""
    claimed = (
        UserCalendarEvent.query
        .filter(UserCalendarEvent.id == event.id)
        .filter(or_(
            UserCalendarEvent.reminder_sent_for_start_at.is_(None),
            UserCalendarEvent.reminder_sent_for_start_at != event.start_at,
        ))
        .update(
            {
                UserCalendarEvent.reminder_sent_at: now,
                UserCalendarEvent.reminder_sent_for_start_at: event.start_at,
            },
            synchronize_session=False,
        )
    )
    return bool(claimed)


def send_due_user_calendar_reminders(
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> int:
    """Create one in-app and email reminder for each currently due event.

    The marker stores the event's start timestamp.  Rescheduling an event thus
    produces a fresh reminder for its new date while repeated worker polls do
    not recreate an old one.
    """
    now = now or datetime.utcnow()
    try:
        item_limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        item_limit = 100
    horizon = now + timedelta(minutes=MAX_REMINDER_LEAD_MINUTES + 60)

    rows = (
        UserCalendarEvent.query
        .filter(UserCalendarEvent.reminder_minutes_before.isnot(None))
        .filter(UserCalendarEvent.start_at > now)
        .filter(UserCalendarEvent.start_at <= horizon)
        .filter(or_(
            UserCalendarEvent.reminder_sent_for_start_at.is_(None),
            UserCalendarEvent.reminder_sent_for_start_at != UserCalendarEvent.start_at,
        ))
        .order_by(UserCalendarEvent.start_at.asc(), UserCalendarEvent.id.asc())
        .limit(item_limit)
        .all()
    )

    emitted = 0
    for event in rows:
        reminder_at = calendar_event_reminder_at(event)
        if reminder_at is None or reminder_at > now:
            continue
        if not _claim_event_reminder(event, now):
            continue

        notification = Notification(
            user_id=event.owner_user_id,
            message=_event_reminder_message(event),
            type="USER_CALENDAR_REMINDER",
            source="portal",
            is_read=False,
            is_mirror=False,
            created_at=now,
            link_url=_event_calendar_url(event),
            email_delivery_mode=CALENDAR_REMINDER_EMAIL_MODE,
            event_key=(
                f"user-calendar-reminder:{event.id}:"
                f"{event.start_at.strftime('%Y%m%d%H%M%S')}"
            )[:64],
            target_type="USER_CALENDAR_EVENT",
            target_id=event.id,
        )
        db.session.add(notification)
        db.session.flush()
        enqueue_notification_email(notification)
        emitted += 1

    if emitted:
        db.session.commit()
    return emitted
