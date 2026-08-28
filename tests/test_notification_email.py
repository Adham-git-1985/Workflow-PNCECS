import unittest
from unittest.mock import patch

from flask import Flask

from extensions import db
from models import Notification, NotificationEmailDelivery, SystemSetting, User
from services.notification_email import send_pending_notification_emails


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
        db.session.add(self.user)
        db.session.add_all((
            SystemSetting(key="EMAIL_CIRCULAR_ENABLED", value="1"),
            SystemSetting(key="EMAIL_CIRCULAR_SMTP_HOST", value="smtp.example.test"),
            SystemSetting(key="EMAIL_CIRCULAR_SMTP_PORT", value="25"),
            SystemSetting(key="EMAIL_CIRCULAR_SECURITY", value="none"),
            SystemSetting(key="EMAIL_CIRCULAR_FROM_EMAIL", value="masar.pncecs@gmail.com"),
            SystemSetting(key="EMAIL_CIRCULAR_PUBLIC_URL", value="http://10.10.10.204:5000"),
        ))
        db.session.commit()

    def test_new_notification_queues_and_sends_email_with_public_link(self):
        db.session.add(Notification(
            user_id=self.user.id,
            message="تحديث على طلب المواد #12",
            source="portal",
            link_url="http://127.0.0.1:5000/portal/inventory/employee-requests/12",
            is_read=False,
        ))
        db.session.commit()

        with patch("services.notification_email._send_email") as send_email:
            self.assertEqual(send_pending_notification_emails(), 1)

        subject, text_body, html_body = send_email.call_args.args[2:]
        self.assertIn("تحديث على طلب المواد #12", subject)
        self.assertIn("http://10.10.10.204:5000/portal/inventory/employee-requests/12", text_body)
        self.assertIn("http://10.10.10.204:5000/portal/inventory/employee-requests/12", html_body)
        self.assertEqual(NotificationEmailDelivery.query.one().status, "SENT")

    def test_dedicated_and_mirror_notifications_are_not_duplicated(self):
        db.session.add_all((
            Notification(
                user_id=self.user.id,
                message="مهمة جديدة",
                email_delivery_mode="TASK_ASSIGNMENT",
                is_read=False,
            ),
            Notification(
                user_id=self.user.id,
                message="متابعة",
                is_mirror=True,
                is_read=False,
            ),
            Notification(
                user_id=self.user.id,
                message="تعميم",
                email_delivery_mode="DIRECT_EMAIL",
                is_read=False,
            ),
        ))
        db.session.commit()

        self.assertEqual(NotificationEmailDelivery.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
