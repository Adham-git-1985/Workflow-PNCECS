import tempfile
import unittest
from pathlib import Path

from flask import Flask, g
from flask_login import LoginManager
from jinja2 import ChoiceLoader, DictLoader

from extensions import db
from models import User, UserPermission, WorkflowInstance, WorkflowInstanceStep, WorkflowRequest
from workflow import workflow_bp


class WorkflowDashboardPermissionTests(unittest.TestCase):
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
            SECRET_KEY="workflow-dashboard-permission-test",
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
        cls.app.jinja_env.filters["workflow_status_label"] = lambda value: value
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
        admin = User(
            email="workflow-dashboard-admin@example.test",
            name="Workflow Dashboard Admin",
            password_hash="not-used-in-test",
            role="SUPER_ADMIN",
        )
        self.employee = User(
            email="workflow-dashboard@example.test",
            name="Workflow Dashboard Employee",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        db.session.add(admin)
        db.session.flush()
        db.session.add(self.employee)
        db.session.commit()

    def _login(self, client):
        for key in ("_login_user", "delegation_checked", "delegations", "delegation", "effective_user"):
            g.pop(key, None)
        with client.session_transaction() as session:
            session.clear()
            session["_user_id"] = str(self.employee.id)
            session["_fresh"] = True

    def test_dashboard_requires_explicit_permission(self):
        self.assertFalse(self.employee.has_perm("WORKFLOW_DASHBOARD_READ"))
        with self.app.test_client() as client:
            self._login(client)
            response = client.get("/workflow/work")

        self.assertEqual(response.status_code, 403)

    def test_dashboard_is_available_when_permission_is_granted(self):
        db.session.add(UserPermission(
            user_id=self.employee.id,
            key="WORKFLOW_DASHBOARD_READ",
            is_allowed=True,
        ))
        db.session.commit()

        with self.app.test_client() as client:
            self._login(client)
            response = client.get("/workflow/work")

        self.assertEqual(response.status_code, 200)

    def test_dashboard_prefers_the_saved_selected_recipient_name(self):
        selected_recipient = User(
            email="selected-recipient@example.test",
            name="Route Candidate",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        db.session.add(selected_recipient)
        db.session.flush()
        request_row = WorkflowRequest(
            requester_id=self.employee.id,
            title="Request with a specifically selected recipient",
            description="",
            status="IN_PROGRESS",
        )
        db.session.add(request_row)
        db.session.flush()
        instance = WorkflowInstance(
            request_id=request_row.id,
            current_step_order=1,
        )
        db.session.add(instance)
        db.session.flush()
        db.session.add(WorkflowInstanceStep(
            instance_id=instance.id,
            step_order=1,
            approver_kind="ROLE",
            approver_role="EMPLOYEE",
            routing_label="The specifically selected recipient",
            status="PENDING",
        ))
        db.session.add(UserPermission(
            user_id=self.employee.id,
            key="WORKFLOW_DASHBOARD_READ",
            is_allowed=True,
        ))
        db.session.commit()

        with self.app.test_client() as client:
            self._login(client)
            response = client.get("/workflow/work?queue=created")

        self.assertEqual(response.status_code, 200)
        self.assertIn("The specifically selected recipient", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
