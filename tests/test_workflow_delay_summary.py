import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from flask import Flask, g
from flask_login import LoginManager
from jinja2 import ChoiceLoader, DictLoader

from extensions import db
from models import (
    Notification,
    User,
    UserPermission,
    WorkflowInstance,
    WorkflowInstanceStep,
    WorkflowRequest,
)
from services.workflow_delay_summary import (
    WORKFLOW_DELAY_SUMMARY_PATH,
    get_overdue_workflow_rows,
    is_workflow_delay_summary_viewer,
    send_daily_workflow_delay_summary,
)
from workflow import workflow_bp


class WorkflowDelaySummaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        project_root = Path(__file__).resolve().parents[1]
        cls.app.template_folder = str(project_root / "templates")
        cls.app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY="workflow-delay-summary-test",
            APP_TIMEZONE="Asia/Jerusalem",
        )
        db.init_app(cls.app)
        login_manager = LoginManager()
        login_manager.init_app(cls.app)

        @login_manager.user_loader
        def load_user(user_id):
            try:
                return db.session.get(User, int(user_id))
            except (TypeError, ValueError):
                return None

        cls.app.register_blueprint(workflow_bp)
        cls.app.jinja_loader = ChoiceLoader([
            DictLoader({"layout.html": "{% block content %}{% endblock %}"}),
            cls.app.jinja_loader,
        ])
        cls.app.jinja_env.filters["workflow_status_label"] = lambda value: value
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

    @staticmethod
    def _user(email, role="EMPLOYEE"):
        user = User(
            email=email,
            username=email,
            password_hash="not-used-in-test",
            role=role,
        )
        db.session.add(user)
        db.session.flush()
        return user

    @staticmethod
    def _login(client, user):
        for key in ("_login_user", "delegation_checked", "delegations", "delegation", "effective_user"):
            g.pop(key, None)
        with client.session_transaction() as session:
            session.clear()
            session["_user_id"] = str(user.id)
            session["_fresh"] = True

    def test_overdue_rows_and_daily_recipients_are_idempotent(self):
        requester = self._user("delay-requester@example.test")
        secretary = self._user("delay-secretary@example.test", role="GENERAL_SECRETARY")
        super_admin = self._user("delay-super@example.test", role="SUPER_ADMIN")
        observer = self._user("delay-observer@example.test", role="ADMIN")
        db.session.add(UserPermission(
            user_id=observer.id,
            key="NOTIFICATIONS_GLOBAL_OBSERVER",
            is_allowed=True,
        ))

        request_row = WorkflowRequest(
            title="طلب متأخر للاختبار",
            description="اختبار التقرير اليومي",
            status="IN_PROGRESS",
            requester_id=requester.id,
            created_at=datetime(2026, 9, 20, 5, 30),
        )
        db.session.add(request_row)
        db.session.flush()
        instance = WorkflowInstance(
            request_id=request_row.id,
            current_step_order=1,
            is_completed=False,
        )
        db.session.add(instance)
        db.session.flush()
        db.session.add(WorkflowInstanceStep(
            instance_id=instance.id,
            step_order=1,
            status="PENDING",
            approver_kind="ROLE",
            approver_role="DEPT_HEAD",
            sla_days=1,
            due_at=datetime(2026, 9, 22, 5, 30),
        ))
        db.session.commit()

        rows = get_overdue_workflow_rows(now=datetime(2026, 9, 23, 5, 30))
        self.assertEqual([row["request"].id for row in rows], [request_row.id])

        first = send_daily_workflow_delay_summary(
            now=datetime(2026, 9, 23, 5, 30),
            force=True,
        )
        self.assertEqual(first["sent"], 3)
        self.assertEqual(first["delayed_count"], 1)

        second = send_daily_workflow_delay_summary(
            now=datetime(2026, 9, 23, 5, 30),
            force=True,
        )
        self.assertEqual(second["sent"], 0)
        self.assertEqual(second["status"], "already_sent")

        notifications = Notification.query.filter(
            Notification.event_key == first["event_key"],
            Notification.is_mirror.is_(False),
        ).all()
        self.assertEqual({row.user_id for row in notifications}, {
            secretary.id,
            super_admin.id,
            observer.id,
        })
        self.assertTrue(all(row.link_url == WORKFLOW_DELAY_SUMMARY_PATH for row in notifications))
        self.assertTrue(all(row.type == "WARNING" for row in notifications))

    def test_job_does_not_send_before_0830_local_time(self):
        self._user("before-schedule@example.test", role="GENERAL_SECRETARY")
        result = send_daily_workflow_delay_summary(
            now=datetime(2026, 9, 23, 4, 29),
        )
        self.assertEqual(result["status"], "before_schedule")
        self.assertEqual(Notification.query.count(), 0)

    def test_report_is_limited_to_the_daily_audience_and_links_to_the_request(self):
        requester = self._user("report-requester@example.test")
        secretary = self._user("report-secretary@example.test", role="GENERAL_SECRETARY")
        request_row = WorkflowRequest(
            title="طلب ظاهر في تقرير التأخير",
            status="IN_PROGRESS",
            requester_id=requester.id,
            created_at=datetime(2026, 9, 20, 5, 30),
        )
        db.session.add(request_row)
        db.session.flush()
        instance = WorkflowInstance(
            request_id=request_row.id,
            current_step_order=1,
            is_completed=False,
        )
        db.session.add(instance)
        db.session.flush()
        db.session.add(WorkflowInstanceStep(
            instance_id=instance.id,
            step_order=1,
            status="PENDING",
            approver_kind="ROLE",
            approver_role="DEPT_HEAD",
            sla_days=1,
            due_at=datetime(2026, 9, 22, 5, 30),
        ))
        db.session.commit()
        self.assertFalse(is_workflow_delay_summary_viewer(requester))

        with self.app.test_client() as client:
            self._login(client, secretary)
            report = client.get("/workflow/delayed")

            self.assertEqual(report.status_code, 200)
            self.assertIn("طلب ظاهر في تقرير التأخير".encode("utf-8"), report.data)
            self.assertIn(
                f"/workflow/request/{request_row.id}".encode("utf-8"),
                report.data,
            )
            with patch("workflow.routes.render_template", return_value="ok"):
                detail = client.get(f"/workflow/request/{request_row.id}")
            self.assertEqual(detail.status_code, 200)

        with self.app.test_client() as requester_client:
            self._login(requester_client, requester)
            self.assertEqual(requester_client.get("/workflow/delayed").status_code, 403)


if __name__ == "__main__":
    unittest.main()
