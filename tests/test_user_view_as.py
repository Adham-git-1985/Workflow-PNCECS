import tempfile
import unittest
from pathlib import Path

from flask import Flask, g
from flask_login import LoginManager

from extensions import db
from models import Notification, User, UserPermission
from users import users_bp


class UserViewAsRouteTests(unittest.TestCase):
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
            SECRET_KEY="view-as-test",
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

        cls.app.register_blueprint(users_bp)
        cls.app.jinja_env.filters["ui_label"] = lambda value: value or "—"
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

        self.super_admin = User(
            email="view-as-super@example.test",
            name="View As SuperAdmin",
            password_hash="not-used-in-test",
            role="SUPER_ADMIN",
        )
        self.admin = User(
            email="view-as-admin@example.test",
            name="Regular Admin",
            password_hash="not-used-in-test",
            role="ADMIN",
        )
        self.employee = User(
            email="view-as-employee@example.test",
            name="Preview Employee",
            job_title="Employee",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        db.session.add_all((self.super_admin, self.admin, self.employee))
        db.session.flush()
        db.session.add(UserPermission(
            user_id=self.employee.id,
            key="PORTAL_READ",
            is_allowed=True,
        ))
        db.session.commit()

    @staticmethod
    def _login(client, user_id):
        g.pop("_login_user", None)
        with client.session_transaction() as session:
            session.clear()
            session["_user_id"] = str(user_id)
            session["_fresh"] = True

    def test_superadmin_can_open_read_only_preview_without_session_or_notification_changes(self):
        client = self.app.test_client()
        self._login(client, self.super_admin.id)

        response = client.get(f"/users/{self.employee.id}/view-as")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Preview Employee", body)
        self.assertIn("البوابة الإدارية", body)
        self.assertIn("لا يظهر", body)
        self.assertIn("PORTAL_READ", body)
        self.assertNotIn("setInterval", body)
        self.assertNotIn("notification_alerts", body)
        self.assertEqual(Notification.query.count(), 0)

        with client.session_transaction() as session:
            self.assertEqual(session.get("_user_id"), str(self.super_admin.id))
            self.assertNotIn("view_as_user_id", session)

    def test_regular_admin_cannot_open_view_as(self):
        client = self.app.test_client()
        self._login(client, self.admin.id)

        response = client.get(f"/users/{self.employee.id}/view-as")

        self.assertEqual(response.status_code, 403)

    def test_view_as_does_not_mutate_target(self):
        client = self.app.test_client()
        self._login(client, self.super_admin.id)
        before = (self.employee.name, self.employee.role, self.employee.email)

        response = client.get(f"/users/{self.employee.id}/view-as")

        self.assertEqual(response.status_code, 200)
        db.session.expire_all()
        target = db.session.get(User, self.employee.id)
        self.assertEqual((target.name, target.role, target.email), before)


if __name__ == "__main__":
    unittest.main()
