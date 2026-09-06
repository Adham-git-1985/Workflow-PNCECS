from datetime import date, datetime
import tempfile
import unittest
from pathlib import Path

from flask import Flask
from flask_login import LoginManager
from jinja2 import ChoiceLoader, DictLoader

from extensions import db
from models import (
    AuditLog,
    EmployeeFile,
    EmployeeFollowupReport,
    Notification,
    User,
    UserPermission,
    WorkflowRequest,
)
from portal import portal_bp
from portal.followups import _report_docx_filename


class FollowupFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        project_root = Path(__file__).resolve().parents[1]
        cls.app = Flask(
            __name__,
            instance_path=cls.temp_dir.name,
            template_folder=str(project_root / "templates"),
        )
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="followup-flow-test",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(cls.app)
        login_manager = LoginManager(cls.app)

        @login_manager.user_loader
        def load_user(user_id):
            return db.session.get(User, int(user_id))

        cls.app.register_blueprint(portal_bp)
        cls.app.jinja_loader = ChoiceLoader([
            DictLoader({"portal/layout.html": "{% block content %}{% endblock %}"}),
            cls.app.jinja_loader,
        ])
        cls.app.jinja_env.globals["csrf_token"] = lambda: "test-token"

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def setUp(self):
        with self.app.app_context():
            db.drop_all()
            db.create_all()
            manager = User(
                email="followup-manager@example.test",
                name="Manager",
                password_hash="not-used-in-test",
                role="EMPLOYEE",
            )
            employee = User(
                email="followup-employee@example.test",
                name="Employee",
                password_hash="not-used-in-test",
                role="EMPLOYEE",
            )
            db.session.add_all([manager, employee])
            db.session.flush()
            self.manager_id = manager.id
            self.employee_id = employee.id
            db.session.add_all([
                UserPermission(
                    user_id=self.employee_id,
                    key="FOLLOWUPS_CREATE",
                    is_allowed=True,
                ),
                UserPermission(
                    user_id=self.employee_id,
                    key="FOLLOWUPS_READ",
                    is_allowed=True,
                ),
                UserPermission(
                    user_id=self.manager_id,
                    key="FOLLOWUPS_REVIEW",
                    is_allowed=True,
                ),
                EmployeeFile(
                    user_id=self.employee_id,
                    direct_manager_user_id=self.manager_id,
                ),
            ])
            db.session.commit()

        self.employee_client = self.app.test_client()
        self.manager_client = self.app.test_client()
        with self.employee_client.session_transaction() as session:
            session["_user_id"] = str(self.employee_id)
            session["_fresh"] = True
        with self.manager_client.session_transaction() as session:
            session["_user_id"] = str(self.manager_id)
            session["_fresh"] = True

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()

    def test_direct_manager_can_review_only_after_employee_submits(self):
        response = self.employee_client.post(
            "/portal/followups/new",
            data={"period_start": "2026-09-01", "period_end": "2026-09-05"},
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            report_id = EmployeeFollowupReport.query.one().id

        self.assertEqual(
            self.manager_client.get(f"/portal/followups/{report_id}").status_code,
            403,
        )

        response = self.employee_client.post(
            f"/portal/followups/{report_id}/update",
            data={"action": "submit"},
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            report = db.session.get(EmployeeFollowupReport, report_id)
            self.assertEqual(report.status, "SUBMITTED")
            self.assertEqual(
                Notification.query.filter_by(
                    user_id=self.manager_id,
                    type="FOLLOWUP_SUBMITTED",
                ).count(),
                1,
            )

        response = self.manager_client.post(
            f"/portal/followups/{report_id}/review",
            data={
                "action": "review",
                "manager_comment": "Reviewed",
                "manager_rating": "GOOD",
            },
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            report = db.session.get(EmployeeFollowupReport, report_id)
            self.assertEqual(report.status, "REVIEWED")
            self.assertEqual(report.manager_comment, "Reviewed")

    def test_user_with_followups_read_can_delete_any_report(self):
        with self.app.app_context():
            report = EmployeeFollowupReport(
                employee_user_id=self.manager_id,
                period_start=date(2026, 9, 1),
                period_end=date(2026, 9, 5),
                status="DRAFT",
            )
            db.session.add(report)
            db.session.commit()
            report_id = report.id

        response = self.employee_client.post(f"/portal/followups/{report_id}/delete")

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertIsNone(db.session.get(EmployeeFollowupReport, report_id))

    def test_new_report_imports_completed_masar_work(self):
        with self.app.app_context():
            workflow_request = WorkflowRequest(
                requester_id=self.employee_id,
                title="معاملة لاختبار الاستيراد",
                status="IN_PROGRESS",
            )
            db.session.add(workflow_request)
            db.session.flush()
            db.session.add(AuditLog(
                request_id=workflow_request.id,
                user_id=self.employee_id,
                action="STEP_APPROVED",
                created_at=datetime(2026, 9, 3, 10, 30),
            ))
            db.session.commit()

        response = self.employee_client.post(
            "/portal/followups/new",
            data={"period_start": "2026-09-01", "period_end": "2026-09-05"},
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            report = EmployeeFollowupReport.query.one()
            imported_item = next(
                item for item in report.items if item.source_type == "WORKFLOW_AUDIT"
            )
            self.assertIn("متابعة واعتماد خطوة", imported_item.title)
            self.assertIn("معاملة لاختبار الاستيراد", imported_item.title)
            self.assertEqual(imported_item.completed_on.isoformat(), "2026-09-03")

        response = self.employee_client.post(
            f"/portal/followups/{report.id}/import-workflow"
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertEqual(
                EmployeeFollowupReport.query.one().items[0].source_type,
                "WORKFLOW_AUDIT",
            )
            self.assertEqual(
                len(EmployeeFollowupReport.query.one().items),
                1,
            )

    def test_word_export_filename_uses_the_requested_period_format(self):
        report = EmployeeFollowupReport(
            period_start=datetime(2026, 9, 1).date(),
            period_end=datetime(2026, 9, 6).date(),
        )

        self.assertEqual(
            _report_docx_filename(report),
            "تقرير_انجاز_من_2026-09-01_الى_2026-09-06.docx",
        )


if __name__ == "__main__":
    unittest.main()
