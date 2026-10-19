import tempfile
import unittest
from datetime import datetime
from pathlib import Path

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
    WorkflowRequestView,
)
from utils.ui_labels import ui_label, workflow_status_label
from workflow import workflow_bp
from workflow.reports import build_employee_workflow_report


class WorkflowReportsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        project_root = Path(__file__).resolve().parents[1]
        cls.app = Flask(
            __name__,
            instance_path=cls.temp_dir.name,
            template_folder=str(project_root / "templates"),
            static_folder=str(project_root / "static"),
        )
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="workflow-reports-test",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
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
        cls.app.jinja_env.filters["ui_label"] = ui_label
        cls.app.jinja_env.filters["workflow_status_label"] = workflow_status_label
        cls.context = cls.app.app_context()
        cls.context.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.context.pop()
        cls.temp_dir.cleanup()

    def setUp(self):
        db.session.remove()
        db.drop_all()
        db.create_all()
        self.employee = User(
            email="workflow-report-employee@example.test",
            name="Employee Report",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        self.requester = User(
            email="workflow-report-requester@example.test",
            name="Report Requester",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        db.session.add_all([self.employee, self.requester])
        db.session.flush()

        request_row = WorkflowRequest(
            requester_id=self.requester.id,
            title="مسار تقرير تجريبي",
            description="طلب تجريبي للتقرير",
            status="IN_PROGRESS",
            created_at=datetime(2026, 10, 1, 8, 0),
        )
        db.session.add(request_row)
        db.session.flush()
        instance = WorkflowInstance(
            request_id=request_row.id,
            current_step_order=1,
            is_completed=False,
            created_at=datetime(2026, 10, 1, 8, 0),
        )
        db.session.add(instance)
        db.session.flush()
        db.session.add(WorkflowInstanceStep(
            instance_id=instance.id,
            step_order=1,
            approver_kind="USER",
            approver_user_id=self.employee.id,
            status="PENDING",
        ))
        db.session.add(WorkflowRequestView(
            user_id=self.employee.id,
            request_id=request_row.id,
            step_order=1,
            viewed_at=datetime(2026, 10, 2, 10, 30),
        ))
        db.session.add(Notification(
            user_id=self.employee.id,
            type="WORKFLOW",
            source="workflow",
            target_type="WorkflowRequest",
            target_id=request_row.id,
            email_delivery_mode="TASK_ASSIGNMENT",
            is_mirror=False,
            created_at=datetime(2026, 10, 1, 9, 15),
            message="طلب جديد يحتاج إجراء",
        ))
        db.session.commit()
        self.request_row = request_row

    def _login(self, client, user=None):
        user = user or self.employee
        for key in ("_login_user", "delegation_checked", "delegations", "delegation", "effective_user"):
            g.pop(key, None)
        with client.session_transaction() as session:
            session.clear()
            session["_user_id"] = str(user.id)
            session["_fresh"] = True

    def test_report_requires_its_explicit_permission(self):
        with self.app.test_client() as client:
            self._login(client)
            response = client.get("/workflow/reports")

        self.assertEqual(response.status_code, 403)

    def test_report_summarizes_assignment_and_open_time(self):
        db.session.add(UserPermission(
            user_id=self.employee.id,
            key="WORKFLOW_REPORTS_READ",
            is_allowed=True,
        ))
        db.session.commit()

        report = build_employee_workflow_report(
            self.employee,
            selected_user_id=self.employee.id,
        )
        self.assertEqual(report["total_paths"], 1)
        self.assertEqual(report["active_assignments"], 1)
        self.assertEqual(report["opened_assignments"], 1)
        self.assertEqual(report["selected_user"].id, self.employee.id)
        self.assertEqual(len(report["selected_details"]), 1)
        detail = report["selected_details"][0]
        self.assertEqual(detail["assignment_at"], datetime(2026, 10, 1, 9, 15))
        self.assertEqual(detail["opened_at"], datetime(2026, 10, 2, 10, 30))

        with self.app.test_client() as client:
            self._login(client)
            response = client.get(
                f"/workflow/reports?user_id={self.employee.id}"
            )

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("مسار تقرير تجريبي", body)
        self.assertIn("وقت فتح الموظف", body)


if __name__ == "__main__":
    unittest.main()
