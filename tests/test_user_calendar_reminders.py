import unittest
from datetime import datetime, time
from unittest.mock import patch

from flask import Flask

from extensions import db
from models import Notification, NotificationEmailDelivery, SystemSetting, User, UserCalendarEvent
from services.notification_email import send_pending_notification_emails
from services.user_calendar import local_datetime_to_utc
from services.user_calendar_reminders import (
    calendar_event_reminder_at,
    send_due_user_calendar_reminders,
)
from utils.timezone import to_local_time


class UserCalendarReminderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY="user-calendar-reminder-test",
            APP_TIMEZONE="Asia/Jerusalem",
        )
        db.init_app(cls.app)
        cls.context = cls.app.app_context()
        cls.context.push()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.context.pop()

    def setUp(self):
        db.session.remove()
        db.drop_all()
        db.create_all()
        self.user = User(
            email="calendar-recipient@example.test",
            name="Calendar Recipient",
            password_hash="unused",
            role="EMPLOYEE",
        )
        db.session.add_all((
            self.user,
            SystemSetting(key="EMAIL_CIRCULAR_ENABLED", value="1"),
            SystemSetting(key="EMAIL_CIRCULAR_SMTP_HOST", value="smtp.example.test"),
            SystemSetting(key="EMAIL_CIRCULAR_SMTP_PORT", value="25"),
            SystemSetting(key="EMAIL_CIRCULAR_SECURITY", value="none"),
            SystemSetting(key="EMAIL_CIRCULAR_FROM_EMAIL", value="no-reply@example.test"),
        ))
        db.session.commit()

    def test_due_reminder_creates_in_app_notification_and_email_outbox_once(self):
        now = datetime(2026, 10, 20, 9, 0)
        event = UserCalendarEvent(
            owner_user_id=self.user.id,
            title="متابعة خطة العمل",
            event_type="TASK",
            start_at=datetime(2026, 10, 21, 8, 30),
            all_day=False,
            reminder_minutes_before=1440,
        )
        db.session.add(event)
        db.session.commit()

        self.assertEqual(send_due_user_calendar_reminders(now=now), 1)
        notification = Notification.query.one()
        self.assertEqual(notification.user_id, self.user.id)
        self.assertEqual(notification.type, "USER_CALENDAR_REMINDER")
        self.assertEqual(notification.email_delivery_mode, "CALENDAR_REMINDER")
        self.assertIn("/portal/calendar?date=", notification.link_url)
        self.assertEqual(NotificationEmailDelivery.query.count(), 1)

        with patch("services.notification_email._send_email") as send_email:
            self.assertEqual(send_pending_notification_emails(now=now), 1)
        send_email.assert_called_once()
        self.assertIn("تذكير بموعد", send_email.call_args.args[2])

        self.assertEqual(send_due_user_calendar_reminders(now=now), 0)
        self.assertEqual(Notification.query.count(), 1)

    def test_all_day_default_reminder_is_sent_at_nine_am_the_day_before(self):
        event = UserCalendarEvent(
            owner_user_id=self.user.id,
            title="مؤتمر يوم كامل",
            event_type="CONFERENCE",
            start_at=local_datetime_to_utc(datetime(2026, 10, 21)),
            all_day=True,
            reminder_minutes_before=1440,
        )
        reminder_at = calendar_event_reminder_at(event)
        reminder_local = to_local_time(reminder_at)

        self.assertEqual(reminder_local.date().isoformat(), "2026-10-20")
        self.assertEqual(reminder_local.time().replace(tzinfo=None), time(9, 0))


if __name__ == "__main__":
    unittest.main()
