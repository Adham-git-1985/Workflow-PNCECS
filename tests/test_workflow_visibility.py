import unittest
from unittest.mock import patch

from flask import Flask

from extensions import db
from models import (
    Committee,
    CommitteeAssignee,
    User,
    WorkflowInstance,
    WorkflowInstanceStep,
    WorkflowRequest,
)
from workflow import workflow_bp
from workflow import routes as workflow_routes
from workflow.routes import _user_can_view_request


class WorkflowVisibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY="test-only",
        )
        db.init_app(cls.app)
        cls.app.register_blueprint(workflow_bp)
        cls.context = cls.app.app_context()
        cls.context.push()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.context.pop()

    def setUp(self):
        db.drop_all()
        db.create_all()

    @staticmethod
    def _user(email, role="EMPLOYEE"):
        user = User(
            email=email,
            password_hash="not-used-in-test",
            role=role,
        )
        db.session.add(user)
        db.session.flush()
        return user

    def _request_with_user_step(self, requester, approver):
        request = WorkflowRequest(
            title="Workflow visibility test",
            status="IN_PROGRESS",
            requester_id=requester.id,
            confidentiality="NORMAL",
        )
        db.session.add(request)
        db.session.flush()

        instance = WorkflowInstance(request_id=request.id, current_step_order=1)
        db.session.add(instance)
        db.session.flush()

        db.session.add(WorkflowInstanceStep(
            instance_id=instance.id,
            step_order=1,
            mode="SEQUENTIAL",
            approver_kind="USER",
            approver_user_id=approver.id,
            status="PENDING",
        ))
        db.session.commit()
        return request

    def test_first_user_without_super_admin_role_cannot_view_unrelated_path(self):
        unrelated_director = self._user(
            "director@example.test",
            role="directorate_head",
        )
        requester = self._user("requester@example.test")
        approver = self._user("approver@example.test")
        request = self._request_with_user_step(requester, approver)

        self.assertEqual(unrelated_director.id, 1)
        self.assertFalse(unrelated_director.has_role("SUPER_ADMIN"))
        self.assertFalse(_user_can_view_request(unrelated_director, request))
        self.assertTrue(_user_can_view_request(approver, request))

    def test_explicit_super_admin_can_view_any_path(self):
        requester = self._user("requester@example.test")
        approver = self._user("approver@example.test")
        explicit_super_admin = self._user(
            "super-admin@example.test",
            role="SUPER_ADMIN",
        )
        request = self._request_with_user_step(requester, approver)

        self.assertTrue(explicit_super_admin.has_role("SUPER_ADMIN"))
        self.assertTrue(_user_can_view_request(explicit_super_admin, request))

    def test_closed_committee_workflow_appears_in_following_for_member(self):
        requester = self._user("requester@example.test")
        committee_member = self._user("committee-member@example.test")
        committee = Committee(name_ar="لجنة الجرد", is_active=True)
        db.session.add(committee)
        db.session.flush()
        db.session.add(CommitteeAssignee(
            committee_id=committee.id,
            kind="USER",
            user_id=committee_member.id,
            member_role="MEMBER",
            is_active=True,
        ))

        request = WorkflowRequest(
            title="Closed committee workflow",
            status="CLOSED",
            requester_id=requester.id,
            confidentiality="NORMAL",
        )
        db.session.add(request)
        db.session.flush()
        instance = WorkflowInstance(
            request_id=request.id,
            current_step_order=1,
            is_completed=True,
        )
        db.session.add(instance)
        db.session.flush()
        db.session.add(WorkflowInstanceStep(
            instance_id=instance.id,
            step_order=1,
            mode="SEQUENTIAL",
            approver_kind="COMMITTEE",
            approver_committee_id=committee.id,
            committee_delivery_mode="Committee_MEMBERS",
            status="APPROVED",
        ))
        db.session.commit()

        with self.app.test_request_context("/workflow/following?summary_filter=closed"):
            with (
                patch.object(workflow_routes, "current_user", committee_member),
                patch.object(workflow_routes, "get_effective_user", return_value=committee_member),
                patch.object(workflow_routes, "get_active_delegations", return_value=[]),
                patch.object(
                    workflow_routes,
                    "render_template",
                    side_effect=lambda _template, **context: context,
                ),
            ):
                context = workflow_routes.following.__wrapped__()

        self.assertEqual([row[0].id for row in context["rows"]], [request.id])


if __name__ == "__main__":
    unittest.main()
