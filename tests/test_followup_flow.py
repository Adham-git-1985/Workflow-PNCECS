import tempfile
import unittest
from pathlib import Path

from flask import Flask
from flask_login import LoginManager
from jinja2 import ChoiceLoader, DictLoader

from extensions import db
from models import EmployeeFile, EmployeeFollowupReport, Notification, User, UserPermission
from portal import portal_bp


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


if __name__ == "__main__":
    unittest.main()
