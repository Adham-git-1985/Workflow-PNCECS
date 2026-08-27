import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, g
from flask_login import LoginManager
from jinja2 import ChoiceLoader, DictLoader

from extensions import db
from models import AuditLog, Notification, User, UserPresence
from users import users_bp
from utils.request_audit import AUTOMATED_ENDPOINTS


class UserPresenceRouteTests(unittest.TestCase):
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
            SECRET_KEY="user-presence-test",
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
        cls.app.jinja_loader = ChoiceLoader([
            DictLoader({"layout.html": "{% block content %}{% endblock %}"}),
            cls.app.jinja_loader,
        ])
        cls.app.jinja_env.globals["csrf_token"] = lambda: "test-token"
        cls.app.jinja_env.filters["ui_label"] = lambda value: value
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

        self.admin = User(
            email="admin-presence@example.test",
            name="Presence Admin",
            password_hash="not-used-in-test",
            role="ADMIN",
        )
        self.active_user = User(
            email="active-user@example.test",
            name="Active User",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        self.stale_user = User(
            email="stale-user@example.test",
            name="Stale User",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        db.session.add_all((self.admin, self.active_user, self.stale_user))
        db.session.flush()
        db.session.add_all((
            UserPresence(
                user_id=self.active_user.id,
                last_seen_at=datetime.utcnow() - timedelta(seconds=30),
                last_path="/workflow/work",
            ),
            UserPresence(
                user_id=self.stale_user.id,
                last_seen_at=datetime.utcnow() - timedelta(minutes=10),
                last_path="/portal",
            ),
        ))
        db.session.commit()

    def _login(self, client, user_id):
        g.pop("_login_user", None)
        with client.session_transaction() as session:
            session.clear()
            session["_user_id"] = str(user_id)
            session["_fresh"] = True

    def test_heartbeat_upserts_current_user_presence(self):
        client = self.app.test_client()
        self._login(client, self.admin.id)

        response = client.post(
            "/users/presence/heartbeat",
            json={"path": "/admin/dashboard"},
        )

        self.assertEqual(response.status_code, 204)
        presence = db.session.get(UserPresence, self.admin.id)
        self.assertIsNotNone(presence)
        self.assertEqual(presence.last_path, "/admin/dashboard")
        self.assertIn("users.presence_heartbeat", AUTOMATED_ENDPOINTS)

    def test_active_users_page_is_admin_only_and_excludes_stale_users(self):
        client = self.app.test_client()
        self._login(client, self.active_user.id)
        self.assertEqual(client.get("/users/active-now").status_code, 403)

        self._login(client, self.admin.id)
        response = client.get("/users/active-now")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"active-user@example.test", response.data)
        self.assertNotIn(b"stale-user@example.test", response.data)

    def test_super_admin_can_open_active_users_page(self):
        self.admin.role = "SUPER_ADMIN"
        db.session.commit()
        client = self.app.test_client()
        self._login(client, self.admin.id)

        self.assertEqual(client.get("/users/active-now").status_code, 200)

    def test_maintenance_notice_targets_only_other_active_users(self):
        client = self.app.test_client()
        self._login(client, self.admin.id)

        response = client.post(
            "/users/active-now/notify",
            data={"message": "Save your work before restart."},
        )

        self.assertEqual(response.status_code, 302)
        notifications = Notification.query.order_by(Notification.user_id.asc()).all()
        self.assertEqual([row.user_id for row in notifications], [self.active_user.id])
        self.assertIn("Save your work before restart.", notifications[0].message)
        self.assertIsNotNone(AuditLog.query.filter_by(
            action="ACTIVE_USERS_MAINTENANCE_NOTICE"
        ).first())


if __name__ == "__main__":
    unittest.main()
