import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask, g
from flask_login import LoginManager
from jinja2 import ChoiceLoader, DictLoader

from extensions import db
from models import (
    AuditLog,
    OrgNode,
    OrgNodeManager,
    OrgNodeType,
    RolePermission,
    User,
    UserPermission,
    WorkflowInstance,
    WorkflowInstanceStep,
    WorkflowRequest,
    WorkflowStepTask,
)
from workflow import workflow_bp
from workflow.routes import MENTION_ACCESS_ACTION


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

    def _login(self, client, user=None):
        user = user or self.employee
        for key in ("_login_user", "delegation_checked", "delegations", "delegation", "effective_user"):
            g.pop(key, None)
        with client.session_transaction() as session:
            session.clear()
            session["_user_id"] = str(user.id)
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

    def test_general_secretary_alias_inherits_access_and_role_tasks(self):
        secretary = User(
            email="general-secretary-dashboard@example.test",
            name="General Secretary",
            password_hash="not-used-in-test",
            role="General_secretary",
        )
        db.session.add(secretary)
        db.session.flush()
        request_row = WorkflowRequest(
            requester_id=self.employee.id,
            title="General Secretary role alias task",
            description="",
            status="IN_PROGRESS",
        )
        db.session.add(request_row)
        db.session.flush()
        instance = WorkflowInstance(
            request_id=request_row.id,
            current_step_order=1,
            is_completed=False,
        )
        db.session.add(instance)
        db.session.flush()
        db.session.add_all((
            WorkflowInstanceStep(
                instance_id=instance.id,
                step_order=1,
                approver_kind="ROLE",
                approver_role="SECRETARY_GENERAL",
                status="PENDING",
            ),
            RolePermission(
                role="GENERAL-SECRETARY",
                permission="WORKFLOW_DASHBOARD_READ",
            ),
        ))
        db.session.commit()

        with self.app.test_client() as client:
            self._login(client, secretary)
            dashboard = client.get("/workflow/work?queue=my_action")
            inbox = client.get("/workflow/inbox")

        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(inbox.status_code, 200)
        self.assertIn(request_row.title.encode("utf-8"), dashboard.data)
        self.assertIn(request_row.title.encode("utf-8"), inbox.data)

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

    def test_org_node_manager_sees_pending_step_in_inbox(self):
        assistant = User(
            email="assistant-secretary@example.test",
            name="Assistant Secretary General",
            password_hash="not-used-in-test",
            role="ASSISTANT_SECRETARY_GENERAL",
        )
        node_type = OrgNodeType(
            code="SEC_GEN_ASSIST",
            name_ar="مساعد الأمين العام",
            is_active=True,
        )
        db.session.add_all((assistant, node_type))
        db.session.flush()
        assistant_node = OrgNode(
            type_id=node_type.id,
            name_ar="مساعد الأمين العام للتخطيط والخدمات المساندة",
            is_active=True,
        )
        db.session.add(assistant_node)
        db.session.flush()
        db.session.add(OrgNodeManager(
            node_id=assistant_node.id,
            manager_user_id=assistant.id,
        ))

        request_row = WorkflowRequest(
            requester_id=self.employee.id,
            title="طلب بانتظار مساعد الأمين العام",
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
            approver_kind="ORG_NODE",
            approver_org_node_id=assistant_node.id,
            status="PENDING",
        ))
        db.session.commit()

        with self.app.test_client() as client:
            self._login(client, assistant)
            response = client.get("/workflow/inbox")

        self.assertEqual(response.status_code, 200)
        self.assertIn(request_row.title.encode("utf-8"), response.data)

    def test_prior_approver_sees_returning_route_in_following_not_inbox(self):
        secretary = User(
            email="prior-secretary@example.test",
            name="Prior Secretary",
            password_hash="not-used-in-test",
            role="GENERAL_SECRETARY",
        )
        current_manager = User(
            email="current-route-manager@example.test",
            name="Current Route Manager",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        node_type = OrgNodeType(
            code="RETURN_ROUTE",
            name_ar="مسار عائد",
            is_active=True,
        )
        db.session.add_all((secretary, current_manager, node_type))
        db.session.flush()
        root_node = OrgNode(type_id=node_type.id, name_ar="المستوى الأعلى", is_active=True)
        db.session.add(root_node)
        db.session.flush()
        current_node = OrgNode(
            type_id=node_type.id,
            parent_id=root_node.id,
            name_ar="المستوى الحالي",
            is_active=True,
        )
        db.session.add(current_node)
        db.session.flush()
        db.session.add_all((
            OrgNodeManager(node_id=root_node.id, manager_user_id=secretary.id),
            OrgNodeManager(node_id=current_node.id, manager_user_id=current_manager.id),
        ))

        request_row = WorkflowRequest(
            requester_id=self.employee.id,
            title="Returning route follows instead of inbox",
            description="",
            status="IN_PROGRESS",
        )
        db.session.add(request_row)
        db.session.flush()
        instance = WorkflowInstance(
            request_id=request_row.id,
            current_step_order=2,
            is_completed=False,
        )
        db.session.add(instance)
        db.session.flush()
        db.session.add_all((
            WorkflowInstanceStep(
                instance_id=instance.id,
                step_order=1,
                mode="SEQUENTIAL",
                approver_kind="USER",
                approver_user_id=secretary.id,
                approver_org_node_id=current_node.id,
                status="APPROVED",
                decided_by_id=secretary.id,
            ),
            WorkflowInstanceStep(
                instance_id=instance.id,
                step_order=2,
                mode="SEQUENTIAL",
                approver_kind="ORG_NODE",
                approver_org_node_id=current_node.id,
                status="PENDING",
            ),
            WorkflowInstanceStep(
                instance_id=instance.id,
                step_order=3,
                mode="SEQUENTIAL",
                approver_kind="ORG_NODE",
                approver_org_node_id=root_node.id,
                status="PENDING",
            ),
        ))
        db.session.commit()

        with self.app.test_client() as client:
            self._login(client, secretary)
            with patch("workflow.routes.render_template", return_value="ok") as render:
                inbox_response = client.get("/workflow/inbox")
                inbox_rows = render.call_args.kwargs["rows"]
            with patch("workflow.routes.render_template", return_value="ok") as render:
                following_response = client.get("/workflow/following")
                following_rows = render.call_args.kwargs["rows"]

        self.assertEqual(inbox_response.status_code, 200)
        self.assertEqual(following_response.status_code, 200)
        self.assertNotIn(request_row.id, {row[0].id for row in inbox_rows})
        self.assertIn(request_row.id, {row[0].id for row in following_rows})

    def test_pending_mention_from_an_earlier_step_stays_in_my_tasks(self):
        approver = User(
            email="workflow-next-approver@example.test",
            name="Workflow Next Approver",
            password_hash="not-used-in-test",
            role="MANAGER",
        )
        db.session.add(approver)
        db.session.flush()
        request_row = WorkflowRequest(
            requester_id=approver.id,
            title="Mention follow-up after workflow progression",
            description="",
            status="IN_PROGRESS",
        )
        db.session.add(request_row)
        db.session.flush()
        instance = WorkflowInstance(
            request_id=request_row.id,
            current_step_order=2,
            is_completed=False,
        )
        db.session.add(instance)
        db.session.flush()
        db.session.add_all((
            WorkflowInstanceStep(
                instance_id=instance.id,
                step_order=1,
                approver_kind="USER",
                approver_user_id=approver.id,
                status="APPROVED",
            ),
            WorkflowInstanceStep(
                instance_id=instance.id,
                step_order=2,
                approver_kind="USER",
                approver_user_id=approver.id,
                status="PENDING",
            ),
            WorkflowStepTask(
                instance_id=instance.id,
                request_id=request_row.id,
                step_order=1,
                assignee_user_id=self.employee.id,
                status="PENDING",
                response="NONE",
                note="MENTION_TASK",
            ),
            AuditLog(
                request_id=request_row.id,
                user_id=approver.id,
                action=MENTION_ACCESS_ACTION,
                target_type="USER",
                target_id=self.employee.id,
            ),
            UserPermission(
                user_id=self.employee.id,
                key="WORKFLOW_DASHBOARD_READ",
                is_allowed=True,
            ),
        ))
        db.session.commit()

        with self.app.test_client() as client:
            self._login(client)
            inbox = client.get("/workflow/inbox")
            dashboard = client.get("/workflow/work?queue=my_action")

        self.assertEqual(inbox.status_code, 200)
        self.assertIn(request_row.title.encode("utf-8"), inbox.data)
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(request_row.title.encode("utf-8"), dashboard.data)


if __name__ == "__main__":
    unittest.main()
