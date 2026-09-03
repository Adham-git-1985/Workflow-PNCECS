import unittest
from unittest.mock import patch

from flask import Flask

from extensions import db
from models import Notification, NotificationEmailDelivery, SystemSetting, User
from services.notification_email import (
    NOTIFICATION_EMAILS_DISABLED_REASON,
    send_pending_notification_emails,
)


class NotificationEmailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY="notification-email-test",
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
        db.drop_all()
        db.create_all()
        self.user = User(
            email="recipient@example.test",
            name="Recipient",
            password_hash="unused",
            role="EMPLOYEE",
        )
        db.session.add_all((
            self.user,
            SystemSetting(key="EMAIL_CIRCULAR_ENABLED", value="1"),
            SystemSetting(key="EMAIL_CIRCULAR_SMTP_HOST", value="smtp.example.test"),
            SystemSetting(key="EMAIL_CIRCULAR_SMTP_PORT", value="25"),
            SystemSetting(key="EMAIL_CIRCULAR_SECURITY", value="none"),
            SystemSetting(key="EMAIL_CIRCULAR_FROM_EMAIL", value="masar.pncecs@gmail.com"),
        ))
        db.session.commit()

    def test_new_notification_stays_in_the_system_without_an_email_outbox_row(self):
        notification = Notification(
            user_id=self.user.id,
            message="In-app notification only",
            source="portal",
            is_read=False,
        )
        db.session.add(notification)
        db.session.commit()

        self.assertTrue(db.session.get(Notification, notification.id).is_visible)
        self.assertEqual(NotificationEmailDelivery.query.count(), 0)
        with patch("services.notification_email._send_email") as send_email:
            self.assertEqual(send_pending_notification_emails(), 0)
        send_email.assert_not_called()

    def test_legacy_pending_notification_email_is_cancelled_without_sending(self):
        notification = Notification(
            user_id=self.user.id,
            message="Old queued notification",
            source="portal",
            is_read=False,
        )
        db.session.add(notification)
        db.session.flush()
        delivery = NotificationEmailDelivery(
            notification_id=notification.id,
            user_id=self.user.id,
            status="PENDING",
            attempt_count=0,
        )
        db.session.add(delivery)
        db.session.commit()

        with patch("services.notification_email._send_email") as send_email:
            self.assertEqual(send_pending_notification_emails(), 0)
        send_email.assert_not_called()

        delivery = NotificationEmailDelivery.query.one()
        self.assertEqual(delivery.status, "CANCELLED")
        self.assertEqual(delivery.attempt_count, 0)
        self.assertEqual(delivery.last_error, NOTIFICATION_EMAILS_DISABLED_REASON)


if __name__ == "__main__":
    unittest.main()
