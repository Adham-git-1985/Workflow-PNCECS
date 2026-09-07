import unittest

from flask import Flask, g
from flask_login import LoginManager

from extensions import db
from models import (
    AuditLog,
    OrgNode,
    OrgNodeManager,
    OrgNodeType,
    User,
    WorkflowInstance,
    WorkflowInstanceStep,
    WorkflowRequest,
    WorkflowTemplate,
)
from workflow import workflow_bp
from workflow.routes import (
    ASSISTANT_SECRETARY_STEP_REDIRECT_ACTION,
    _user_can_act_on_step,
    _user_can_view_request,
)


class AssistantSecretaryRedirectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="assistant-secretary-redirect-test",
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

        self.requester = self._user("requester@example.test", "Requester", "EMPLOYEE")
        self.assistant = self._user(
            "assistant@example.test",
            "Assistant Secretary General",
            "ASSISTANT_SECRETARY_GENERAL",
        )
        self.general_manager = self._user(
            "general-manager@example.test",
            "General Manager",
            "GENERAL_MANAGER",
        )

        assistant_type = OrgNodeType(
            code="SEC_GEN_ASSIST",
            name_ar="مساعد الأمين العام",
            is_active=True,
        )
        general_type = OrgNodeType(
            code="GENERAL_DIRECTOR",
            name_ar="مدير عام",
            is_active=True,
        )
        db.session.add_all((assistant_type, general_type))
        db.session.flush()

        self.assistant_node = OrgNode(
            type_id=assistant_type.id,
            name_ar="مساعد الأمين العام للشؤون الإدارية",
            is_active=True,
        )
        db.session.add(self.assistant_node)
        db.session.flush()
        self.general_node = OrgNode(
            type_id=general_type.id,
            parent_id=self.assistant_node.id,
            name_ar="الإدارة العامة للشؤون الإدارية",
            is_active=True,
        )
        db.session.add(self.general_node)
        db.session.flush()
        db.session.add_all((
            OrgNodeManager(
                node_id=self.assistant_node.id,
                manager_user_id=self.assistant.id,
            ),
            OrgNodeManager(
                node_id=self.general_node.id,
                manager_user_id=self.general_manager.id,
            ),
        ))
        db.session.commit()

    @staticmethod
    def _user(email, name, role):
        user = User(
            email=email,
            name=name,
            password_hash="not-used-in-test",
            role=role,
        )
        db.session.add(user)
        db.session.flush()
        return user

    def _login(self, client):
        g.pop("_login_user", None)
        with client.session_transaction() as session:
            session.clear()
            session["_user_id"] = str(self.assistant.id)
            session["_fresh"] = True

    def _create_request(self, *, dynamic):
        template = None
        if not dynamic:
            template = WorkflowTemplate(
                name="مسار مسبق التعريف",
                created_by_id=self.requester.id,
            )
            db.session.add(template)
            db.session.flush()

        request_row = WorkflowRequest(
            requester_id=self.requester.id,
            title="طلب تحويل مساعد الأمين العام",
            status="IN_PROGRESS",
            confidentiality="NORMAL",
        )
        db.session.add(request_row)
        db.session.flush()
        instance = WorkflowInstance(
            request_id=request_row.id,
            template_id=template.id if template else None,
            current_step_order=1,
        )
        db.session.add(instance)
        db.session.flush()
        step = WorkflowInstanceStep(
            instance_id=instance.id,
            step_order=1,
            approver_kind="ORG_NODE" if dynamic else "USER",
            approver_org_node_id=self.assistant_node.id if dynamic else None,
            approver_user_id=None if dynamic else self.assistant.id,
            status="PENDING",
        )
        db.session.add(step)
        db.session.commit()
        return request_row, step

    def _assert_redirect(self, *, dynamic):
        request_row, step = self._create_request(dynamic=dynamic)
        with self.app.test_client() as client:
            self._login(client)
            response = client.post(
                f"/workflow/request/{request_row.id}/step/{step.step_order}/redirect-to-general-manager",
                data={
                    "target_node_id": str(self.general_node.id),
                    "note": "للاختصاص والمتابعة",
                },
            )

        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        redirected_step = db.session.get(WorkflowInstanceStep, step.id)
        self.assertEqual(redirected_step.status, "PENDING")
        self.assertEqual(redirected_step.approver_kind, "ORG_NODE")
        self.assertEqual(redirected_step.approver_org_node_id, self.general_node.id)
        self.assertIsNone(redirected_step.approver_user_id)
        self.assertTrue(_user_can_act_on_step(self.general_manager, redirected_step))
        self.assertTrue(_user_can_view_request(self.assistant, request_row))
        self.assertIsNotNone(AuditLog.query.filter_by(
            request_id=request_row.id,
            action=ASSISTANT_SECRETARY_STEP_REDIRECT_ACTION,
        ).first())

    def test_predefined_route_redirects_to_general_manager(self):
        self._assert_redirect(dynamic=False)

    def test_dynamic_route_redirects_to_general_manager(self):
        self._assert_redirect(dynamic=True)


if __name__ == "__main__":
    unittest.main()
