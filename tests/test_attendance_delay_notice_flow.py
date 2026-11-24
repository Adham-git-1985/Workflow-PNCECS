import os
import unittest
from datetime import datetime
from pathlib import Path

from docx import Document
from flask import Flask
from flask_login import LoginManager, login_user
from flask_wtf.csrf import generate_csrf

from extensions import db
from assistant import assistant_bp
from models import (
    AttendanceDailySummary,
    ArchivedFile,
    EmployeeFile,
    HRAttendanceDelayRequest,
    Notification,
    User,
    WorkflowInstance,
)
from portal import portal_bp
from users import users_bp
from workflow import workflow_bp


class AttendanceDelayNoticeFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(
            __name__,
            template_folder=str(Path(__file__).resolve().parents[1] / "templates"),
        )
        cls.app.config.update(
            SECRET_KEY="attendance-delay-notice-test",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            WTF_CSRF_ENABLED=False,
        )
        db.init_app(cls.app)
        cls.login_manager = LoginManager(cls.app)
        cls.app.add_template_global(generate_csrf, "csrf_token")

        @cls.login_manager.user_loader
        def _load_user(user_id):
            return db.session.get(User, int(user_id))

        @cls.app.route("/_test/notice-login/<int:user_id>")
        def _test_login(user_id):
            login_user(db.session.get(User, user_id))
            return "ok"

        @cls.app.route("/logout", endpoint="logout")
        def _test_logout():
            return "logout"

        cls.app.register_blueprint(workflow_bp)
        cls.app.register_blueprint(portal_bp)
        cls.app.register_blueprint(users_bp)
        cls.app.register_blueprint(assistant_bp)
        cls.context = cls.app.app_context()
        cls.context.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.context.pop()

    def setUp(self):
        db.session.remove()
        db.drop_all()
        db.create_all()
        self.client = self.app.test_client()
        self.admin = User(email="notice-admin@example.test", name="Notice HR", password_hash="x", role="SUPER_ADMIN")
        self.secretary = User(email="notice-secretary@example.test", name="Notice Secretary", password_hash="x", role="GENERAL_SECRETARY")
        self.employee = User(email="notice-employee@example.test", name="Notice Employee", password_hash="x", role="EMPLOYEE")
        self.assignee = User(
            email="notice-assignee@example.test",
            name="Notice Administrative Employee",
            password_hash="x",
            role="EMPLOYEE",
            job_title="موظف الشؤون الإدارية والمالية",
        )
        db.session.add_all((self.admin, self.secretary, self.employee, self.assignee))
        db.session.flush()
        db.session.add(EmployeeFile(user_id=self.employee.id, employee_no="N-100"))
        self.summary = AttendanceDailySummary(
            user_id=self.employee.id,
            day="2026-10-02",
            first_in=datetime(2026, 10, 2, 8, 42),
            late_minutes=42,
            early_leave_minutes=0,
            status="OK",
        )
        db.session.add(self.summary)
        db.session.commit()

    def tearDown(self):
        for archived in ArchivedFile.query.all():
            path = getattr(archived, "file_path", None)
            if path and os.path.exists(path):
                os.remove(path)
        db.session.rollback()

    def _login(self, user):
        self.client.get(f"/_test/notice-login/{user.id}")

    def test_notice_moves_employee_secretary_assignee_and_back_without_workflow_instance(self):
        self._login(self.admin)
        started = self.client.post(
            f"/portal/hr/reports/attendance/delay/{self.summary.id}/start",
        )
        self.assertEqual(started.status_code, 302)
        case = HRAttendanceDelayRequest.query.one()
        self.assertIsNone(WorkflowInstance.query.filter_by(request_id=case.workflow_request_id).first())
        self.assertEqual(case.case_status, "PENDING_EMPLOYEE")
        employee_notice = Notification.query.filter_by(user_id=self.employee.id).one()
        self.assertEqual(employee_notice.target_type, "HRAttendanceDelayRequest")
        self.assertIn(f"/portal/hr/attendance/delay/{case.id}", employee_notice.link_url)

        self._login(self.employee)
        opened = self.client.get(f"/portal/hr/attendance/delay/{case.id}")
        self.assertEqual(opened.status_code, 200)
        submitted = self.client.post(
            f"/portal/hr/reports/attendance/delay/{case.id}/respond",
            data={"reason": "ازدحام مروري طارئ", "has_document": "NO"},
        )
        self.assertEqual(submitted.status_code, 302)
        db.session.refresh(case)
        self.assertEqual(case.case_status, "PENDING_SECRETARY_GENERAL")
        self.assertEqual(case.response_payload()["reason"], "ازدحام مروري طارئ")
        self.assertTrue(Notification.query.filter_by(user_id=self.secretary.id).count())

        self._login(self.secretary)
        assigned = self.client.post(
            f"/portal/hr/attendance/delay/{case.id}/secretary-action",
            data={"action": "ASSIGN", "assignee_id": str(self.assignee.id), "note": "يرجى اتخاذ الإجراء الإداري"},
        )
        self.assertEqual(assigned.status_code, 302)
        db.session.refresh(case)
        self.assertEqual(case.case_status, "ASSIGNED_ADMIN_FINANCE")
        self.assertEqual(case.current_assignee_id, self.assignee.id)

        self._login(self.assignee)
        action = self.client.post(
            f"/portal/hr/attendance/delay/{case.id}/administrative-action",
            data={"note": "تمت مراجعة الحالة واتخاذ الإجراء اللازم."},
        )
        self.assertEqual(action.status_code, 302)
        db.session.refresh(case)
        self.assertEqual(case.case_status, "RETURNED_TO_SECRETARY_GENERAL")
        self.assertIsNone(case.current_assignee_id)

        self._login(self.secretary)
        closed = self.client.post(
            f"/portal/hr/attendance/delay/{case.id}/secretary-action",
            data={"action": "CLOSE", "note": "تم الإنهاء بعد الاطلاع على الإجراء."},
        )
        self.assertEqual(closed.status_code, 302)
        db.session.refresh(case)
        self.assertEqual(case.case_status, "CLOSED")
        self.assertEqual(case.workflow_request.status, "CLOSED")
        self.assertIsNotNone(case.completed_justification_archived_file_id)
        document = Document(case.completed_justification_archived_file.file_path)
        document_text = "\n".join(
            [paragraph.text for paragraph in document.paragraphs]
            + [
                paragraph.text
                for table in document.tables
                for row in table.rows
                for cell in row.cells
                for paragraph in cell.paragraphs
            ]
        )
        self.assertIn("ازدحام مروري طارئ", document_text)

    def test_detailed_collection_pdf_is_removed_and_summary_remains(self):
        self._login(self.admin)
        self.client.post(f"/portal/hr/reports/attendance/delay/{self.summary.id}/start")
        detailed = self.client.get("/portal/hr/reports/attendance/delay-workflow.pdf")
        self.assertEqual(detailed.status_code, 404)
        summary = self.client.get("/portal/hr/reports/attendance/delay-workflow-summary.pdf")
        self.assertEqual(summary.status_code, 200)
        self.assertEqual(summary.mimetype, "application/pdf")


if __name__ == "__main__":
    unittest.main()
