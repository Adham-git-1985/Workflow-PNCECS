import unittest
from datetime import date, datetime
from unittest.mock import patch

from flask import Flask

from extensions import db
from models import (
    SystemSetting,
    User,
    WorkflowInstance,
    WorkflowInstanceStep,
    WorkflowRequest,
    WorkflowStepTask,
    WorkflowTaskEmailDelivery,
)
from services.workflow_task_email import (
    ASSIGNMENT,
    DAILY_REMINDER,
    SENT,
    enqueue_daily_task_reminders,
    enqueue_task_assignment_emails,
    run_workflow_task_email_cycle,
    send_pending_task_emails,
)


class WorkflowTaskEmailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY="workflow-task-email-test",
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

        self.requester = User(
            email="requester@example.test",
            name="Requester",
            password_hash="unused",
            role="EMPLOYEE",
        )
        self.assignee = User(
            email="assignee@example.test",
            name="Assignee",
            password_hash="unused",
            role="EMPLOYEE",
        )
        db.session.add_all((self.requester, self.assignee))
        db.session.flush()

        self.request = WorkflowRequest(
            requester_id=self.requester.id,
            title="مهمة اختبار البريد",
            status="IN_PROGRESS",
            confidentiality="NORMAL",
        )
        db.session.add(self.request)
        db.session.flush()

        self.instance = WorkflowInstance(
            request_id=self.request.id,
            current_step_order=1,
            is_completed=False,
        )
        db.session.add(self.instance)
        db.session.flush()
        db.session.add(WorkflowInstanceStep(
            instance_id=self.instance.id,
            step_order=1,
            mode="SEQUENTIAL",
            approver_kind="USER",
            approver_user_id=self.assignee.id,
            status="PENDING",
        ))
        db.session.add_all((
            SystemSetting(key="EMAIL_CIRCULAR_ENABLED", value="1"),
            SystemSetting(key="EMAIL_CIRCULAR_SMTP_HOST", value="smtp.example.test"),
            SystemSetting(key="EMAIL_CIRCULAR_SMTP_PORT", value="25"),
            SystemSetting(key="EMAIL_CIRCULAR_SECURITY", value="none"),
            SystemSetting(key="EMAIL_CIRCULAR_USERNAME", value=""),
            SystemSetting(key="EMAIL_CIRCULAR_FROM_EMAIL", value="masar.pncecs@gmail.com"),
            SystemSetting(key="EMAIL_CIRCULAR_FROM_NAME", value="نظام مسار"),
            SystemSetting(key="EMAIL_CIRCULAR_PUBLIC_URL", value="https://masar.example.test"),
        ))
        db.session.commit()

    def test_assignment_email_contains_task_number_and_absolute_link(self):
        queued = enqueue_task_assignment_emails(
            self.request,
            [self.assignee.id],
            step_order=1,
            instance_id=self.instance.id,
            link_url=f"http://127.0.0.1:5000/workflow/request/{self.request.id}",
        )
        db.session.commit()
        self.assertEqual(queued, 1)

        with patch("services.workflow_task_email.smtplib.SMTP") as smtp_class:
            self.assertEqual(send_pending_task_emails(), 1)

        message = smtp_class.return_value.send_message.call_args.args[0]
        self.assertEqual(message["To"], self.assignee.email)
        self.assertIn(f"#{self.request.id}", message["Subject"])
        html_body = message.get_body(preferencelist=("html",)).get_content()
        self.assertIn(f"https://masar.example.test/workflow/request/{self.request.id}", html_body)
        delivery = WorkflowTaskEmailDelivery.query.one()
        self.assertEqual(delivery.delivery_kind, ASSIGNMENT)
        self.assertEqual(delivery.status, SENT)

    def test_daily_reminder_is_queued_once_per_calendar_day(self):
        enqueue_task_assignment_emails(
            self.request,
            [self.assignee.id],
            step_order=1,
            instance_id=self.instance.id,
        )
        db.session.commit()
        test_day = date(2026, 8, 28)

        self.assertEqual(enqueue_daily_task_reminders(test_day), 1)
        db.session.commit()
        self.assertEqual(enqueue_daily_task_reminders(test_day), 0)
        db.session.commit()

        reminder = WorkflowTaskEmailDelivery.query.filter_by(
            delivery_kind=DAILY_REMINDER,
            delivery_date=test_day.isoformat(),
        ).one()
        self.assertEqual(reminder.user_id, self.assignee.id)

        with patch("services.workflow_task_email.smtplib.SMTP") as smtp_class:
            self.assertEqual(send_pending_task_emails(), 2)

        messages = [call.args[0] for call in smtp_class.return_value.send_message.call_args_list]
        self.assertTrue(any("تذكير يومي" in message["Subject"] for message in messages))


    def test_pending_task_email_uses_the_current_user_email_address(self):
        enqueue_task_assignment_emails(
            self.request,
            [self.assignee.id],
            step_order=1,
            instance_id=self.instance.id,
        )
        db.session.commit()
        self.assignee.email = "new-assignee@example.test"
        db.session.commit()

        with patch("services.workflow_task_email.smtplib.SMTP") as smtp_class:
            self.assertEqual(send_pending_task_emails(), 1)

        message = smtp_class.return_value.send_message.call_args.args[0]
        self.assertEqual(message["To"], "new-assignee@example.test")

    def test_disabled_global_email_control_skips_task_email_queue_and_smtp(self):
        db.session.add(SystemSetting(key="SYSTEM_EMAIL_DELIVERY_ENABLED", value="0"))
        db.session.commit()

        queued = enqueue_task_assignment_emails(
            self.request,
            [self.assignee.id],
            step_order=1,
            instance_id=self.instance.id,
        )
        db.session.commit()

        self.assertEqual(queued, 0)
        self.assertEqual(WorkflowTaskEmailDelivery.query.count(), 0)
        with patch("services.workflow_task_email.smtplib.SMTP") as smtp_class:
            self.assertEqual(send_pending_task_emails(), 0)
        smtp_class.assert_not_called()

    def test_daily_reminder_includes_pending_mentioned_user(self):
        mentioned = User(
            email="mentioned@example.test",
            name="Mentioned User",
            password_hash="unused",
            role="EMPLOYEE",
        )
        db.session.add(mentioned)
        db.session.flush()
        db.session.add(WorkflowStepTask(
            instance_id=self.instance.id,
            request_id=self.request.id,
            step_order=1,
            assignee_user_id=mentioned.id,
            status="PENDING",
            response="NONE",
            note="MENTION_TASK",
        ))
        db.session.commit()

        self.assertEqual(enqueue_daily_task_reminders(date(2026, 8, 28)), 2)
        db.session.commit()
        recipient_ids = {
            row.user_id
            for row in WorkflowTaskEmailDelivery.query.filter_by(
                delivery_kind=DAILY_REMINDER,
                delivery_date="2026-08-28",
            ).all()
        }
        self.assertEqual(recipient_ids, {self.assignee.id, mentioned.id})

    def test_daily_reminder_starts_at_0830(self):
        with patch("services.workflow_task_email.send_pending_task_emails", return_value=0):
            before_time = run_workflow_task_email_cycle(datetime(2026, 8, 28, 8, 29))
            at_time = run_workflow_task_email_cycle(datetime(2026, 8, 28, 8, 30))

        self.assertEqual(before_time["queued_reminders"], 0)
        self.assertEqual(at_time["queued_reminders"], 1)


if __name__ == "__main__":
    unittest.main()
