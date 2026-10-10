import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from flask import Flask

from extensions import db
from models import (
    HRLeaveRequest,
    HRLeaveType,
    HRRequestApprovalStep,
    Notification,
    NotificationEmailDelivery,
    SystemSetting,
    User,
)
from services.notification_email import (
    ATTENDANCE_SCHEDULE_EMAIL_MODE,
    HR_REQUEST_EMAIL_MODE,
    HR_REQUEST_RECIPIENT_CANCELLED_REASON,
    NOTIFICATION_EMAILS_DISABLED_REASON,
    enqueue_notification_email,
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

    def test_attendance_schedule_notification_is_queued_and_sent(self):
        notification = Notification(
            user_id=self.user.id,
            message="Attendance schedule approved",
            source="portal",
            link_url="/portal/hr/attendance/work-schedule",
            email_delivery_mode=ATTENDANCE_SCHEDULE_EMAIL_MODE,
            is_read=False,
        )
        db.session.add(notification)
        db.session.flush()

        self.assertTrue(enqueue_notification_email(notification))
        db.session.commit()

        delivery = NotificationEmailDelivery.query.one()
        self.assertEqual(delivery.status, "PENDING")
        with patch("services.notification_email._send_email") as send_email:
            self.assertEqual(send_pending_notification_emails(), 1)
        send_email.assert_called_once()

        delivery = NotificationEmailDelivery.query.one()
        self.assertEqual(delivery.status, "SENT")
        self.assertIsNotNone(delivery.sent_at)

    def test_non_hr_email_with_hr_shaped_link_is_not_subject_to_hr_guard(self):
        notification = Notification(
            user_id=self.user.id,
            message="Attendance schedule approved",
            source="portal",
            link_url="/portal/hr/approvals/leaves/999999",
            email_delivery_mode=ATTENDANCE_SCHEDULE_EMAIL_MODE,
            is_read=False,
        )
        db.session.add(notification)
        db.session.flush()
        self.assertTrue(enqueue_notification_email(notification))
        db.session.commit()

        with patch("services.notification_email._send_email") as send_email:
            self.assertEqual(send_pending_notification_emails(), 1)
        send_email.assert_called_once()

        delivery = NotificationEmailDelivery.query.one()
        self.assertEqual(delivery.status, "SENT")

    def test_hr_request_notification_for_missing_request_is_cancelled(self):
        notification = Notification(
            user_id=self.user.id,
            message="Leave request is waiting for your approval",
            source="portal",
            link_url="/portal/hr/approvals/leaves/123",
            email_delivery_mode=HR_REQUEST_EMAIL_MODE,
            is_read=False,
        )
        db.session.add(notification)
        db.session.flush()

        self.assertTrue(enqueue_notification_email(notification))
        db.session.commit()

        with patch("services.notification_email._send_email") as send_email:
            self.assertEqual(send_pending_notification_emails(), 0)
        send_email.assert_not_called()

        delivery = NotificationEmailDelivery.query.one()
        self.assertEqual(delivery.user_id, self.user.id)
        self.assertEqual(delivery.status, "CANCELLED")
        self.assertEqual(delivery.attempt_count, 0)
        self.assertEqual(
            delivery.last_error,
            HR_REQUEST_RECIPIENT_CANCELLED_REASON,
        )

    def test_hr_request_email_without_a_valid_request_link_is_cancelled(self):
        notification = Notification(
            user_id=self.user.id,
            message="Leave request update",
            source="portal",
            link_url=None,
            email_delivery_mode=HR_REQUEST_EMAIL_MODE,
            is_read=False,
        )
        db.session.add(notification)
        db.session.flush()
        self.assertTrue(enqueue_notification_email(notification))
        db.session.commit()

        with patch("services.notification_email._send_email") as send_email:
            self.assertEqual(send_pending_notification_emails(), 0)
        send_email.assert_not_called()

        delivery = NotificationEmailDelivery.query.one()
        self.assertEqual(delivery.status, "CANCELLED")
        self.assertEqual(delivery.attempt_count, 0)
        self.assertEqual(
            delivery.last_error,
            HR_REQUEST_RECIPIENT_CANCELLED_REASON,
        )

    def test_hr_request_email_allows_requester_and_current_approver(self):
        requester = User(
            email="requester@example.test",
            name="Requester",
            password_hash="unused",
            role="EMPLOYEE",
        )
        leave_type = HRLeaveType(code="ANNUAL", name_ar="Annual")
        db.session.add_all((requester, leave_type))
        db.session.flush()
        leave_request = HRLeaveRequest(
            user_id=requester.id,
            leave_type_id=leave_type.id,
            start_date="2026-10-10",
            end_date="2026-10-10",
            status="SUBMITTED",
        )
        db.session.add(leave_request)
        db.session.flush()
        db.session.add(HRRequestApprovalStep(
            request_kind="LEAVE",
            request_id=leave_request.id,
            flow_revision=1,
            step_order=1,
            stage_code="DIRECT_MANAGER",
            approver_scope="USER",
            approver_user_id=self.user.id,
            approver_user_ids=f"[{self.user.id}]",
            status="PENDING",
        ))
        notifications = (
            Notification(
                user_id=requester.id,
                message="Your leave request was submitted",
                source="portal",
                link_url=f"/portal/hr/approvals/leaves/{leave_request.id}",
                email_delivery_mode=HR_REQUEST_EMAIL_MODE,
                is_read=False,
            ),
            Notification(
                user_id=self.user.id,
                message="Leave request is waiting for your approval",
                source="portal",
                link_url=f"/portal/hr/approvals/leaves/{leave_request.id}",
                email_delivery_mode=HR_REQUEST_EMAIL_MODE,
                is_read=False,
            ),
        )
        db.session.add_all(notifications)
        db.session.flush()
        for notification in notifications:
            self.assertTrue(enqueue_notification_email(notification))
        db.session.commit()

        with patch("services.notification_email._send_email") as send_email:
            self.assertEqual(send_pending_notification_emails(), 2)

        self.assertEqual(send_email.call_count, 2)
        self.assertEqual(
            {delivery.status for delivery in NotificationEmailDelivery.query.all()},
            {"SENT"},
        )

    def test_hr_request_email_cancels_unrelated_or_stale_recipient(self):
        requester = User(
            email="requester@example.test",
            name="Requester",
            password_hash="unused",
            role="EMPLOYEE",
        )
        current_approver = User(
            email="current-approver@example.test",
            name="Current approver",
            password_hash="unused",
            role="EMPLOYEE",
        )
        leave_type = HRLeaveType(code="ANNUAL", name_ar="Annual")
        db.session.add_all((requester, current_approver, leave_type))
        db.session.flush()
        leave_request = HRLeaveRequest(
            user_id=requester.id,
            leave_type_id=leave_type.id,
            start_date="2026-10-10",
            end_date="2026-10-10",
            status="SUBMITTED",
        )
        db.session.add(leave_request)
        db.session.flush()
        db.session.add(HRRequestApprovalStep(
            request_kind="LEAVE",
            request_id=leave_request.id,
            flow_revision=1,
            step_order=1,
            stage_code="DIRECT_MANAGER",
            approver_scope="USER",
            approver_user_id=current_approver.id,
            approver_user_ids=f"[{current_approver.id}]",
            status="PENDING",
        ))
        notification = Notification(
            user_id=self.user.id,
            message="Old leave approval assignment",
            source="portal",
            link_url=f"/portal/hr/approvals/leaves/{leave_request.id}",
            email_delivery_mode=HR_REQUEST_EMAIL_MODE,
            is_read=False,
        )
        db.session.add(notification)
        db.session.flush()
        self.assertTrue(enqueue_notification_email(notification))
        db.session.commit()

        with patch("services.notification_email._send_email") as send_email:
            self.assertEqual(send_pending_notification_emails(), 0)
        send_email.assert_not_called()

        delivery = NotificationEmailDelivery.query.one()
        self.assertEqual(delivery.status, "CANCELLED")
        self.assertEqual(delivery.attempt_count, 0)
        self.assertEqual(
            delivery.last_error,
            HR_REQUEST_RECIPIENT_CANCELLED_REASON,
        )

    def test_hr_request_decision_email_allows_the_decision_actor(self):
        requester = User(
            email="requester@example.test",
            name="Requester",
            password_hash="unused",
            role="EMPLOYEE",
        )
        leave_type = HRLeaveType(code="ANNUAL", name_ar="Annual")
        db.session.add_all((requester, leave_type))
        db.session.flush()
        leave_request = HRLeaveRequest(
            user_id=requester.id,
            leave_type_id=leave_type.id,
            start_date="2026-10-10",
            end_date="2026-10-10",
            status="APPROVED",
            decided_by_id=self.user.id,
        )
        db.session.add(leave_request)
        db.session.flush()
        notification = Notification(
            user_id=self.user.id,
            actor_id=self.user.id,
            type="HR_REQUEST_APPROVED",
            message="Leave request approved",
            source="portal",
            link_url=f"/portal/hr/approvals/leaves/{leave_request.id}",
            email_delivery_mode=HR_REQUEST_EMAIL_MODE,
            is_read=False,
        )
        db.session.add(notification)
        db.session.flush()
        self.assertTrue(enqueue_notification_email(notification))
        db.session.commit()

        with patch("services.notification_email._send_email") as send_email:
            self.assertEqual(send_pending_notification_emails(), 1)
        send_email.assert_called_once()

        delivery = NotificationEmailDelivery.query.one()
        self.assertEqual(delivery.status, "SENT")

    def test_notification_email_is_retried_once_and_then_stops(self):
        notification = Notification(
            user_id=self.user.id,
            message="Attendance schedule approved",
            source="portal",
            email_delivery_mode=ATTENDANCE_SCHEDULE_EMAIL_MODE,
            is_read=False,
        )
        db.session.add(notification)
        db.session.flush()
        self.assertTrue(enqueue_notification_email(notification))
        db.session.commit()
        first_attempt = datetime(2026, 8, 28, 8, 0)

        with patch(
            "services.notification_email._send_email",
            side_effect=RuntimeError("mailbox unavailable"),
        ) as send_email:
            self.assertEqual(
                send_pending_notification_emails(now=first_attempt),
                0,
            )
            delivery = NotificationEmailDelivery.query.one()
            self.assertEqual(delivery.status, "PENDING")
            self.assertEqual(delivery.attempt_count, 1)

            self.assertEqual(
                send_pending_notification_emails(
                    now=first_attempt + timedelta(minutes=3),
                ),
                0,
            )
            delivery = NotificationEmailDelivery.query.one()
            self.assertEqual(delivery.status, "FAILED")
            self.assertEqual(delivery.attempt_count, 2)

            self.assertEqual(
                send_pending_notification_emails(
                    now=first_attempt + timedelta(minutes=10),
                ),
                0,
            )

        self.assertEqual(send_email.call_count, 2)


if __name__ == "__main__":
    unittest.main()
