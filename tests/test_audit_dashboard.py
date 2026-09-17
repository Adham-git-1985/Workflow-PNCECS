import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from flask import Flask
from flask_login import LoginManager
from jinja2 import ChoiceLoader, DictLoader

from audit import audit_bp
from audit.routes import _build_audit_dashboard_report
from extensions import db
from models import AuditLog, User


class AuditDashboardTests(unittest.TestCase):
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
            SECRET_KEY="audit-dashboard-test",
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

        cls.app.register_blueprint(audit_bp)
        cls.app.jinja_loader = ChoiceLoader([
            DictLoader({
                "layout.html": "{% block extra_css %}{% endblock %}{% block content %}{% endblock %}"
            }),
            cls.app.jinja_loader,
        ])
        cls.app.jinja_env.filters["ui_label"] = lambda value: str(value or "")
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
        self.admin = self._user("admin@example.test", "مدير النظام", "SUPER_ADMIN")
        self.alice = self._user("alice@example.test", "أليس")
        self.bob = self._user("bob@example.test", "بوب")
        db.session.commit()

    @staticmethod
    def _user(email, name, role="EMPLOYEE"):
        row = User(email=email, name=name, password_hash="not-used-in-test", role=role)
        db.session.add(row)
        db.session.flush()
        return row

    def _audit(self, actor, action, when, **kwargs):
        db.session.add(
            AuditLog(
                user_id=actor.id,
                actual_user_id=kwargs.pop("actual_user_id", actor.id),
                action=action,
                created_at=when,
                **kwargs,
            )
        )

    def _login_admin(self, client):
        with client.session_transaction() as session:
            session["_user_id"] = str(self.admin.id)
            session["_fresh"] = True

    def test_report_counts_real_actor_and_keeps_zero_activity_users(self):
        self._audit(self.alice, "PAGE_VIEW", datetime(2026, 9, 10, 9), module_name="WORKFLOW")
        self._audit(self.alice, "WORKFLOW_STARTED", datetime(2026, 9, 10, 10))
        self._audit(self.alice, "USER_ACTION_FAILED", datetime(2026, 9, 11, 10), module_name="WORKFLOW")
        self._audit(
            self.alice,
            "WORKFLOW_REPLY",
            datetime(2026, 9, 11, 11),
            actual_user_id=self.bob.id,
            acting_for_user_id=self.alice.id,
        )
        db.session.commit()

        with self.app.test_request_context("/audit/dashboard?period=all"):
            report = _build_audit_dashboard_report()

        self.assertEqual(report["summary"]["total_events"], 4)
        self.assertEqual(report["summary"]["active_users"], 2)
        self.assertEqual(report["summary"]["delegated_events"], 1)
        self.assertEqual(report["summary"]["inactive_users"], 1)
        self.assertEqual(report["summary"]["active_rate_pct"], 66.7)

        rows = {row["id"]: row for row in report["users"]}
        self.assertEqual(rows[self.alice.id]["events"], 3)
        self.assertEqual(rows[self.alice.id]["failed_operations"], 1)
        self.assertEqual(rows[self.alice.id]["band_key"], "low")
        self.assertEqual(rows[self.bob.id]["events"], 1)
        self.assertEqual(rows[self.bob.id]["delegated_events"], 1)
        self.assertEqual(rows[self.admin.id]["events"], 0)

        self.assertEqual(report["modules"][0]["label"], "مسار")
        self.assertEqual(report["modules"][0]["count"], 4)

    def test_report_filters_by_actor_and_custom_period(self):
        self._audit(self.alice, "PAGE_VIEW", datetime(2026, 9, 1, 9), module_name="WORKFLOW")
        self._audit(self.alice, "WORKFLOW_STARTED", datetime(2026, 9, 12, 9))
        self._audit(self.bob, "PORTAL_CIRCULAR_CREATE", datetime(2026, 9, 12, 10))
        db.session.commit()

        with self.app.test_request_context(
            f"/audit/dashboard?user_id={self.alice.id}&period=custom&date_from=2026-09-10&date_to=2026-09-12"
        ):
            report = _build_audit_dashboard_report()

        self.assertEqual(report["selected_user_id"], self.alice.id)
        self.assertEqual(report["summary"]["total_events"], 1)
        self.assertEqual(report["summary"]["active_users"], 1)
        self.assertEqual(report["summary"]["scope_users_count"], 1)
        self.assertEqual(report["users"][0]["name"], "أليس")

    def test_dashboard_and_excel_export_render(self):
        self._audit(self.alice, "PAGE_VIEW", datetime(2026, 9, 12, 9), module_name="WORKFLOW")
        db.session.commit()

        with self.app.test_client() as client:
            self._login_admin(client)
            response = client.get("/audit/dashboard?period=all")
            self.assertEqual(response.status_code, 200)
            self.assertIn("استخدام نظام مسار بالأرقام".encode("utf-8"), response.data)
            self.assertIn("استخدام كل مستخدم".encode("utf-8"), response.data)

            export = client.get("/audit/dashboard/export-excel?period=all")
            self.assertEqual(export.status_code, 200)
            self.assertIn(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                export.content_type,
            )


if __name__ == "__main__":
    unittest.main()
