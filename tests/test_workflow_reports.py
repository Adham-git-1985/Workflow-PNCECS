import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from flask import Flask, g
from flask_login import LoginManager
from jinja2 import ChoiceLoader, DictLoader

from extensions import db
from models import (
    HRAttendanceSchedulePlan,
    HRAttendanceSpecialCase,
    HRLeaveRequest,
    HRLeaveType,
    HRPermissionRequest,
    HRPermissionType,
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
from workflow.approval_reports import build_open_approval_report
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
        cls.app.jinja_env.globals["csrf_token"] = lambda: "test-csrf"
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
                f"/workflow/reports/employee-workflow?user_id={self.employee.id}"
            )

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("مسار تقرير تجريبي", body)
        self.assertIn("وقت فتح الموظف", body)


    def test_reports_index_is_a_catalog_with_employee_report_button(self):
        db.session.add(UserPermission(
            user_id=self.employee.id,
            key="WORKFLOW_REPORTS_READ",
            is_allowed=True,
        ))
        db.session.commit()

        with self.app.test_client() as client:
            self._login(client)
            response = client.get("/workflow/reports")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("/workflow/reports/employee-workflow", body)
        self.assertNotIn("workflow-report-search", body)

    def test_email_settings_are_available_only_to_email_managers(self):
        db.session.add(UserPermission(
            user_id=self.employee.id,
            key="WORKFLOW_REPORTS_READ",
            is_allowed=True,
        ))
        db.session.commit()

        with self.app.test_client() as client:
            self._login(client)
            denied = client.get("/workflow/reports/email-settings")
        self.assertEqual(denied.status_code, 403)

        db.session.add(UserPermission(
            user_id=self.employee.id,
            key="WORKFLOW_REPORTS_EMAIL_MANAGE",
            is_allowed=True,
        ))
        db.session.commit()
        with self.app.test_client() as client:
            self._login(client)
            allowed = client.get("/workflow/reports/email-settings")

        self.assertEqual(allowed.status_code, 200)
        self.assertIn("10:00", allowed.get_data(as_text=True))
        self.assertIn("include_open_approvals", allowed.get_data(as_text=True))

    def test_employee_report_contains_separate_print_targets(self):
        db.session.add(UserPermission(
            user_id=self.employee.id,
            key="WORKFLOW_REPORTS_READ",
            is_allowed=True,
        ))
        db.session.commit()

        with self.app.test_client() as client:
            self._login(client)
            response = client.get(
                f"/workflow/reports/employee-workflow?user_id={self.employee.id}"
            )

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("workflow-report-summary-print", body)
        self.assertIn("workflow-report-details", body)
        self.assertIn("workflow-report-details-table", body)
        self.assertIn("wfPrintSmart", body)

    def test_open_approval_report_catalogs_all_pending_sources(self):
        db.session.add(UserPermission(
            user_id=self.employee.id,
            key="WORKFLOW_REPORTS_READ",
            is_allowed=True,
        ))
        leave_type = HRLeaveType(code="TEST_ANNUAL", name_ar="إجازة اختبار")
        permission_type = HRPermissionType(code="TEST_PERMISSION", name_ar="مغادرة اختبار")
        db.session.add_all([leave_type, permission_type])
        db.session.flush()

        submitted_at = datetime(2026, 10, 19, 10, 0)
        db.session.add_all([
            HRLeaveRequest(
                user_id=self.employee.id,
                leave_type_id=leave_type.id,
                start_date="2026-10-20",
                end_date="2026-10-21",
                status="SUBMITTED",
                submitted_at=submitted_at,
            ),
            HRPermissionRequest(
                user_id=self.employee.id,
                permission_type_id=permission_type.id,
                day="2026-10-22",
                from_time="09:00",
                to_time="11:00",
                status="SUBMITTED",
                submitted_at=submitted_at,
            ),
            HRAttendanceSchedulePlan(
                user_id=self.employee.id,
                period_start="2026-10-23",
                period_end="2026-10-29",
                status="SUBMITTED",
                request_type="CHANGE_REQUEST",
                submitted_at=submitted_at,
            ),
            HRAttendanceSpecialCase(
                user_id=self.employee.id,
                day="2026-10-24",
                kind="MATERNITY_DEPARTURE",
                applied=False,
                approval_status="PENDING",
                created_at=submitted_at,
            ),
            HRAttendanceSpecialCase(
                user_id=self.employee.id,
                day="2026-10-25",
                kind="MANUAL_ATTENDANCE",
                start_time="08:00",
                end_time="16:00",
                note="تصحيح اختبار",
                applied=False,
                approval_status="PENDING",
                created_at=submitted_at,
            ),
        ])
        db.session.commit()

        report = build_open_approval_report(self.employee, per_page=None)

        self.assertEqual(report["total_open"], 5)
        self.assertEqual(report["counts"]["LEAVE"], 1)
        self.assertEqual(report["counts"]["PERMISSION"], 1)
        self.assertEqual(report["counts"]["SCHEDULE_CHANGE"], 1)
        self.assertEqual(report["counts"]["MATERNITY_DEPARTURE"], 1)
        self.assertEqual(report["counts"]["DAILY_ATTENDANCE"], 1)
        self.assertEqual(report["max_rows_per_category"], 2000)


if __name__ == "__main__":
    unittest.main()
