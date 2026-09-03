import unittest
from unittest.mock import patch

from flask import Flask

from extensions import db
from models import Notification, NotificationEmailDelivery, SystemSetting, User
from services.delivery_controls import (
    EMAIL_DELIVERY_ENABLED_SETTING,
    NOTIFICATIONS_ENABLED_SETTING,
)
from services.notification_email import send_pending_notification_emails


class DeliveryControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY="delivery-controls-test",
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
            SystemSetting(key="EMAIL_CIRCULAR_FROM_EMAIL", value="no-reply@example.test"),
        ))
        db.session.commit()

    def test_in_app_notifications_can_be_hidden_without_queuing_an_email(self):
        db.session.add_all((
            SystemSetting(key=NOTIFICATIONS_ENABLED_SETTING, value="0"),
            SystemSetting(key=EMAIL_DELIVERY_ENABLED_SETTING, value="1"),
        ))
        db.session.commit()

        notification = Notification(
            user_id=self.user.id,
            message="In-app only delivery",
            source="portal",
        )
        db.session.add(notification)
        db.session.commit()

        self.assertFalse(db.session.get(Notification, notification.id).is_visible)
        self.assertEqual(NotificationEmailDelivery.query.count(), 0)
        with patch("services.notification_email._send_email") as send_email:
            self.assertEqual(send_pending_notification_emails(), 0)
        send_email.assert_not_called()

    def test_disabled_email_control_creates_no_outbox_entry_or_smtp_attempt(self):
        db.session.add(SystemSetting(key=EMAIL_DELIVERY_ENABLED_SETTING, value="0"))
        db.session.commit()

        notification = Notification(
            user_id=self.user.id,
            message="Visible but never emailed",
            source="portal",
        )
        db.session.add(notification)
        db.session.commit()

        self.assertTrue(db.session.get(Notification, notification.id).is_visible)
        self.assertEqual(NotificationEmailDelivery.query.count(), 0)
        with patch("services.notification_email._send_email") as send_email:
            self.assertEqual(send_pending_notification_emails(), 0)
        send_email.assert_not_called()

if __name__ == "__main__":
    unittest.main()
