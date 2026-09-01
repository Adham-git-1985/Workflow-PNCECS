import unittest
from datetime import datetime
from unittest.mock import patch

from flask import Flask

from extensions import db
from models import HRLeaveRequest, HRLeaveType, Notification, NotificationEmailDelivery, SystemSetting, TroubleTicket, User
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

    def test_pending_notification_uses_the_user_current_email_address(self):
        db.session.add(Notification(
            user_id=self.user.id,
            message="Pending notification",
            source="portal",
            is_read=False,
        ))
        db.session.commit()

        self.user.email = "new-recipient@example.test"
        db.session.commit()

        with patch("services.notification_email._send_email") as send_email:
            self.assertEqual(send_pending_notification_emails(), 1)

        self.assertEqual(send_email.call_args.args[1], "new-recipient@example.test")

    def test_notification_email_stops_retrying_after_two_attempts(self):
        db.session.add(Notification(
            user_id=self.user.id,
            message="Retry limit test",
            source="portal",
            is_read=False,
        ))
        db.session.commit()
        first_attempt = datetime(2026, 9, 1, 8, 0)

        with patch(
            "services.notification_email._send_email",
            side_effect=RuntimeError("SMTP unavailable"),
        ) as send_email:
            self.assertEqual(send_pending_notification_emails(now=first_attempt), 0)
            delivery = NotificationEmailDelivery.query.one()
            self.assertEqual(delivery.attempt_count, 1)
            self.assertEqual(delivery.status, "PENDING")

            self.assertEqual(
                send_pending_notification_emails(now=delivery.next_attempt_at),
                0,
            )
            delivery = NotificationEmailDelivery.query.one()
            self.assertEqual(delivery.attempt_count, 2)
            self.assertEqual(delivery.status, "FAILED")

            self.assertEqual(
                send_pending_notification_emails(now=datetime(2026, 9, 2, 8, 0)),
                0,
            )
            self.assertEqual(send_email.call_count, 2)

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

    def test_unauthorized_support_ticket_notification_never_sends_email(self):
        requester = User(
            email="requester@example.test",
            name="Requester",
            password_hash="unused",
            role="EMPLOYEE",
        )
        db.session.add(requester)
        db.session.flush()
        ticket = TroubleTicket(
            requester_id=requester.id,
            subject="Private support ticket",
            description="Ticket details",
            category="SYSTEM",
            priority="NORMAL",
            status="OPEN",
        )
        db.session.add(ticket)
        db.session.flush()
        db.session.add(Notification(
            user_id=self.user.id,
            message="Support ticket update",
            type="TROUBLE_TICKET",
            source="portal",
            link_url=f"/portal/trouble-tickets/{ticket.id}",
            is_read=False,
        ))
        db.session.commit()

        with patch("services.notification_email._send_email") as send_email:
            self.assertEqual(send_pending_notification_emails(), 0)

        send_email.assert_not_called()
        delivery = NotificationEmailDelivery.query.one()
        self.assertEqual(delivery.status, "FAILED")

    def test_ticket_creator_receives_a_direct_reply_email(self):
        ticket = TroubleTicket(
            requester_id=self.user.id,
            subject="Support ticket with an administrator reply",
            description="The requester should receive the reply.",
            category="SYSTEM",
            priority="NORMAL",
            status="IN_PROGRESS",
        )
        db.session.add(ticket)
        db.session.flush()
        db.session.add(Notification(
            user_id=self.user.id,
            message="Administrator replied to your support ticket.",
            type="TROUBLE_TICKET_REQUESTER_UPDATE",
            source="portal",
            link_url=f"/portal/trouble-tickets/{ticket.id}",
            is_read=False,
        ))
        db.session.commit()

        with patch("services.notification_email._send_email") as send_email:
            self.assertEqual(send_pending_notification_emails(), 1)

        self.assertEqual(send_email.call_args.args[1], self.user.email)

    def test_current_ticket_assignee_receives_a_ticket_email(self):
        requester = User(
            email="ticket-requester@example.test",
            name="Ticket Requester",
            password_hash="unused",
            role="EMPLOYEE",
        )
        db.session.add(requester)
        db.session.flush()
        ticket = TroubleTicket(
            requester_id=requester.id,
            assigned_to_id=self.user.id,
            subject="Assigned support ticket",
            description="The assignee should receive this update.",
            category="SYSTEM",
            priority="NORMAL",
            status="IN_PROGRESS",
        )
        db.session.add(ticket)
        db.session.flush()
        db.session.add(Notification(
            user_id=self.user.id,
            message="A support ticket was assigned to you.",
            type="TROUBLE_TICKET",
            source="portal",
            link_url=f"/portal/trouble-tickets/{ticket.id}",
            is_read=False,
        ))
        db.session.commit()

        with patch("services.notification_email._send_email") as send_email:
            self.assertEqual(send_pending_notification_emails(), 1)

        self.assertEqual(send_email.call_args.args[1], self.user.email)

    def test_unauthorized_hr_request_notification_never_sends_email(self):
        requester = User(
            email="leave-requester@example.test",
            name="Leave Requester",
            password_hash="unused",
            role="EMPLOYEE",
        )
        leave_type = HRLeaveType(code="ANNUAL", name_ar="Annual", is_active=True)
        db.session.add_all((requester, leave_type))
        db.session.flush()
        leave = HRLeaveRequest(
            user_id=requester.id,
            leave_type_id=leave_type.id,
            start_date="2026-09-01",
            end_date="2026-09-01",
            status="SUBMITTED",
        )
        db.session.add(leave)
        db.session.flush()
        db.session.add(Notification(
            user_id=self.user.id,
            message="Private leave request update",
            type="HR_APPROVAL",
            source="portal",
            link_url=f"/portal/hr/approvals/leaves/{leave.id}",
            is_read=False,
        ))
        db.session.commit()

        with patch("services.notification_email._send_email") as send_email:
            self.assertEqual(send_pending_notification_emails(), 0)

        send_email.assert_not_called()
        self.assertEqual(NotificationEmailDelivery.query.one().status, "FAILED")


if __name__ == "__main__":
    unittest.main()
