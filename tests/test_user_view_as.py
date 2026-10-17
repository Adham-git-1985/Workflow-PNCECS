import tempfile
import unittest
from pathlib import Path

from flask import Flask, g
from flask_login import LoginManager

from extensions import db
from models import (
    AuditLog,
    Notification,
    User,
    WorkflowInstance,
    WorkflowInstanceStep,
    WorkflowRequest,
    WorkflowRequestView,
)
from users import users_bp
from workflow import workflow_bp


class WorkflowViewAsRouteTests(unittest.TestCase):
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
            SECRET_KEY="workflow-view-as-test",
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
        cls.app.register_blueprint(workflow_bp)
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
            email="workflow-view-as-super@example.test",
            name="Workflow View SuperAdmin",
            password_hash="not-used-in-test",
            role="SUPER_ADMIN",
        )
        self.admin = User(
            email="workflow-view-as-admin@example.test",
            name="Regular Admin",
            password_hash="not-used-in-test",
            role="ADMIN",
        )
        self.target = User(
            email="workflow-view-as-target@example.test",
            name="Workflow Preview Target",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        self.other_user = User(
            email="workflow-view-as-other@example.test",
            name="Other User",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        db.session.add_all((self.super_admin, self.admin, self.target, self.other_user))
        db.session.flush()

        self.request = WorkflowRequest(
            title="Preview workflow request",
            description="Read-only workflow details.",
            status="IN_PROGRESS",
            priority="NORMAL",
            requester_id=self.target.id,
        )
        db.session.add(self.request)
        db.session.flush()
        self.instance = WorkflowInstance(
            request_id=self.request.id,
            current_step_order=1,
            is_completed=False,
        )
        db.session.add(self.instance)
        db.session.flush()
        db.session.add(WorkflowInstanceStep(
            instance_id=self.instance.id,
            step_order=1,
            approver_kind="USER",
            approver_user_id=self.target.id,
            status="PENDING",
        ))
        db.session.add(Notification(
            user_id=self.super_admin.id,
            message=f"Workflow #{self.request.id}",
            type="WORKFLOW",
            is_read=False,
            is_visible=True,
            is_mirror=False,
        ))
        db.session.add(Notification(
            user_id=self.target.id,
            message=f"Workflow #{self.request.id}",
            type="WORKFLOW",
            is_read=False,
            is_visible=True,
            is_mirror=False,
        ))
        db.session.commit()

    @staticmethod
    def _login(client, user_id):
        g.pop("_login_user", None)
        with client.session_transaction() as session:
            session.clear()
            session["_user_id"] = str(user_id)
            session["_fresh"] = True

    def test_superadmin_opens_only_read_only_workflow_details(self):
        client = self.app.test_client()
        self._login(client, self.super_admin.id)

        response = client.get(f"/workflow/view-as/{self.target.id}", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Workflow Preview Target", body)
        self.assertIn("Preview workflow request", body)
        self.assertIn("معاينة تفاصيل المسار", body)
        self.assertNotIn("<form", body.lower())
        self.assertNotIn("method=\"post\"", body.lower())
        self.assertNotIn("notification_alerts", body)
        self.assertNotIn("setInterval", body)

        self.assertEqual(Notification.query.filter_by(is_read=False).count(), 2)
        self.assertEqual(WorkflowRequestView.query.count(), 0)
        self.assertEqual(AuditLog.query.count(), 0)

        with client.session_transaction() as session:
            self.assertEqual(session.get("_user_id"), str(self.super_admin.id))
            self.assertNotIn("view_as_user_id", session)

    def test_non_superadmin_cannot_open_workflow_view_as(self):
        client = self.app.test_client()
        self._login(client, self.admin.id)

        response = client.get(f"/workflow/view-as/{self.target.id}")

        self.assertEqual(response.status_code, 403)

    def test_view_as_does_not_bypass_target_workflow_access(self):
        inaccessible = WorkflowRequest(
            title="Inaccessible request",
            description="Should not be visible to target.",
            status="IN_PROGRESS",
            priority="NORMAL",
            requester_id=self.other_user.id,
        )
        db.session.add(inaccessible)
        db.session.commit()

        client = self.app.test_client()
        self._login(client, self.super_admin.id)

        response = client.get(
            f"/workflow/request/{inaccessible.id}/view-as/{self.target.id}"
        )

        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
