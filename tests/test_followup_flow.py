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
    EmployeeSecondment,
    EmployeeFollowupCopyRecipient,
    EmployeeFollowupItem,
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
            second_manager = User(
                email="followup-second-manager@example.test",
                name="Second Manager",
                password_hash="not-used-in-test",
                role="EMPLOYEE",
            )
            employee = User(
                email="followup-employee@example.test",
                name="Employee",
                password_hash="not-used-in-test",
                role="EMPLOYEE",
            )
            outsider = User(
                email="followup-outsider@example.test",
                name="Outsider",
                password_hash="not-used-in-test",
                role="EMPLOYEE",
            )
            db.session.add_all([manager, second_manager, employee, outsider])
            db.session.flush()
            self.manager_id = manager.id
            self.second_manager_id = second_manager.id
            self.employee_id = employee.id
            self.outsider_id = outsider.id
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
                UserPermission(
                    user_id=self.second_manager_id,
                    key="FOLLOWUPS_REVIEW",
                    is_allowed=True,
                ),
                UserPermission(
                    user_id=self.outsider_id,
                    key="FOLLOWUPS_READ",
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
        self.second_manager_client = self.app.test_client()
        self.outsider_client = self.app.test_client()
        with self.employee_client.session_transaction() as session:
            session["_user_id"] = str(self.employee_id)
            session["_fresh"] = True
        with self.manager_client.session_transaction() as session:
            session["_user_id"] = str(self.manager_id)
            session["_fresh"] = True
        with self.second_manager_client.session_transaction() as session:
            session["_user_id"] = str(self.second_manager_id)
            session["_fresh"] = True
        with self.outsider_client.session_transaction() as session:
            session["_user_id"] = str(self.outsider_id)
            session["_fresh"] = True

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()

    def test_direct_manager_can_review_only_after_employee_submits(self):
        with self.app.app_context():
            db.session.add(UserPermission(
                user_id=self.manager_id,
                key="FOLLOWUPS_MANAGE",
                is_allowed=True,
            ))
            db.session.commit()

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
        dashboard = self.manager_client.get("/portal/followups")
        self.assertEqual(dashboard.status_code, 200)
        self.assertNotIn(b"2026-09-05", dashboard.data)

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

        self.assertEqual(
            self.manager_client.get(f"/portal/followups/{report_id}").status_code,
            200,
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

    def test_employee_can_choose_one_direct_manager_for_submission(self):
        with self.app.app_context():
            db.session.add(EmployeeSecondment(
                user_id=self.employee_id,
                date_from="2000-01-01",
                date_to="2099-12-31",
                direct_manager_user_id=self.second_manager_id,
            ))
            db.session.commit()

        form_page = self.employee_client.get("/portal/followups/new")
        self.assertEqual(form_page.status_code, 200)
        self.assertIn(b"manager_user_id", form_page.data)
        self.assertIn(b"Second Manager", form_page.data)

        response = self.employee_client.post(
            "/portal/followups/new",
            data={
                "period_start": "2026-09-01",
                "period_end": "2026-09-05",
                "manager_user_id": str(self.second_manager_id),
            },
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            report_id = EmployeeFollowupReport.query.one().id
            report = db.session.get(EmployeeFollowupReport, report_id)
            self.assertEqual(report.manager_user_id, self.second_manager_id)

        response = self.employee_client.post(
            f"/portal/followups/{report_id}/update",
            data={"action": "submit"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.manager_client.get(f"/portal/followups/{report_id}").status_code,
            403,
        )
        self.assertEqual(
            self.second_manager_client.get(f"/portal/followups/{report_id}").status_code,
            200,
        )

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

    def test_manual_achievement_accepts_long_text(self):
        long_title = "إنجاز تفصيلي " * 400
        with self.app.app_context():
            report = EmployeeFollowupReport(
                employee_user_id=self.employee_id,
                manager_user_id=self.manager_id,
                period_start=date(2026, 9, 1),
                period_end=date(2026, 9, 5),
                status="DRAFT",
            )
            db.session.add(report)
            db.session.commit()
            report_id = report.id

        response = self.employee_client.post(
            f"/portal/followups/{report_id}/items",
            data={"title": long_title, "completed_on": "2026-09-03"},
        )

        self.assertEqual(response.status_code, 302)
        view = self.employee_client.get(f"/portal/followups/{report_id}")
        self.assertEqual(view.status_code, 200)
        self.assertIn(b'<textarea id="title_', view.data)
        self.assertIn(b'name="title" required rows="5"', view.data)
        with self.app.app_context():
            item = EmployeeFollowupReport.query.get(report_id).items[0]
            self.assertEqual(item.title, long_title.strip())

    def test_employee_can_edit_and_apply_suggestions_to_manual_and_system_items(self):
        with self.app.app_context():
            report = EmployeeFollowupReport(
                employee_user_id=self.employee_id,
                manager_user_id=self.manager_id,
                period_start=date(2026, 9, 1),
                period_end=date(2026, 9, 5),
                status="DRAFT",
            )
            db.session.add(report)
            db.session.flush()
            manual_item = EmployeeFollowupItem(
                report_id=report.id,
                source_type="MANUAL",
                title="قمت بإعداد تقرير مطول عن الأعمال المنجزة",
                status="COMPLETED",
                is_included=True,
            )
            system_item = EmployeeFollowupItem(
                report_id=report.id,
                source_type="WORKFLOW_AUDIT",
                source_id=91,
                title="متابعة واعتماد خطوة: طلب شراء أجهزة\nتفاصيل المعاملة: مراجعة المواصفات",
                status="COMPLETED",
                is_included=True,
            )
            db.session.add_all([manual_item, system_item])
            db.session.commit()
            report_id = report.id
            manual_item_id = manual_item.id
            system_item_id = system_item.id
            manual_title = manual_item.title
            system_title = system_item.title

        response = self.employee_client.post(
            f"/portal/followups/{report_id}/update",
            data={
                "action": "ai",
                f"title_{manual_item_id}": manual_title,
                f"included_{manual_item_id}": "1",
                f"title_{system_item_id}": system_title,
                f"included_{system_item_id}": "1",
            },
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            report = db.session.get(EmployeeFollowupReport, report_id)
            by_id = {item.id: item for item in report.items}
            self.assertTrue(by_id[manual_item_id].ai_suggestion)
            self.assertTrue(by_id[system_item_id].ai_suggestion)
            self.assertEqual(by_id[manual_item_id].title, manual_title)
            self.assertEqual(by_id[system_item_id].title, system_title)

        view = self.employee_client.get(f"/portal/followups/{report_id}")
        self.assertEqual(view.status_code, 200)
        self.assertIn(f'name="ai_suggestion_{manual_item_id}"'.encode(), view.data)
        self.assertIn(f'name="ai_suggestion_{system_item_id}"'.encode(), view.data)
        self.assertIn('value="apply_ai"'.encode(), view.data)

        edited_manual = "تم إعداد تقرير الإنجازات الشهري ومراجعته."
        edited_system = "تمت مراجعة واعتماد طلب شراء الأجهزة."
        response = self.employee_client.post(
            f"/portal/followups/{report_id}/update",
            data={
                "action": "apply_ai",
                f"title_{manual_item_id}": manual_title,
                f"ai_suggestion_{manual_item_id}": edited_manual,
                f"included_{manual_item_id}": "1",
                f"title_{system_item_id}": system_title,
                f"ai_suggestion_{system_item_id}": edited_system,
                f"included_{system_item_id}": "1",
            },
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            report = db.session.get(EmployeeFollowupReport, report_id)
            by_id = {item.id: item for item in report.items}
            self.assertEqual(by_id[manual_item_id].title, edited_manual)
            self.assertEqual(by_id[system_item_id].title, edited_system)
            self.assertIsNone(by_id[manual_item_id].ai_suggestion)
            self.assertIsNone(by_id[system_item_id].ai_suggestion)

    def test_copied_employee_cannot_view_another_employees_report(self):
        with self.app.app_context():
            report = EmployeeFollowupReport(
                employee_user_id=self.employee_id,
                manager_user_id=self.manager_id,
                period_start=date(2026, 9, 1),
                period_end=date(2026, 9, 5),
                status="SUBMITTED",
            )
            db.session.add(report)
            db.session.flush()
            db.session.add(EmployeeFollowupCopyRecipient(
                report_id=report.id,
                user_id=self.outsider_id,
            ))
            db.session.commit()
            report_id = report.id

        response = self.outsider_client.get(f"/portal/followups/{report_id}")

        self.assertEqual(response.status_code, 403)

    def test_new_report_imports_completed_masar_work(self):
        with self.app.app_context():
            workflow_request = WorkflowRequest(
                requester_id=self.employee_id,
                title="معاملة لاختبار الاستيراد",
                description="تفاصيل المعاملة من مسار",
                status="IN_PROGRESS",
            )
            db.session.add(workflow_request)
            db.session.flush()
            db.session.add(AuditLog(
                request_id=workflow_request.id,
                user_id=self.employee_id,
                action="STEP_APPROVED",
                note="ملاحظة الإجراء من مسار",
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
            self.assertIn("تفاصيل المعاملة من مسار", imported_item.title)
            self.assertIn("ملاحظة الإجراء من مسار", imported_item.title)
            self.assertIsNone(imported_item.description)
            self.assertEqual(imported_item.completed_on.isoformat(), "2026-09-03")

        with self.app.app_context():
            audit_log = AuditLog.query.one()
            audit_log.note = "ملاحظة محدّثة من مسار"
            db.session.commit()

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
            self.assertIn(
                "ملاحظة محدّثة من مسار",
                EmployeeFollowupReport.query.one().items[0].title,
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
