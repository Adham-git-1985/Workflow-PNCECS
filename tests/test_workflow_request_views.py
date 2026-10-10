import unittest
from unittest.mock import patch

from flask import Flask

from admin.routes import admin_requests
from extensions import db
from models import User, WorkflowInstance, WorkflowRequest, WorkflowRequestView
from services.workflow_request_views import (
    invalidate_workflow_request_views,
    mark_workflow_request_viewed,
    unopened_workflow_request_ids,
)


class WorkflowRequestViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(cls.app)
        cls.context = cls.app.app_context()
        cls.context.push()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.context.pop()

    def setUp(self):
        db.drop_all()
        db.create_all()
        self.user = User(
            email="workflow-view@example.test",
            name="Workflow Viewer",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        request_row = WorkflowRequest(
            requester=self.user,
            title="Unread workflow request",
            status="IN_PROGRESS",
        )
        db.session.add_all([self.user, request_row])
        db.session.flush()
        self.request_id = request_row.id
        self.instance = WorkflowInstance(
            request_id=request_row.id,
            current_step_order=1,
            is_completed=False,
        )
        db.session.add(self.instance)
        db.session.commit()

    def tearDown(self):
        db.session.remove()

    def test_badge_clears_after_open_and_returns_for_a_new_step(self):
        state = {self.request_id: self.instance.current_step_order}
        self.assertEqual(
            unopened_workflow_request_ids(self.user.id, state),
            {self.request_id},
        )

        self.assertTrue(mark_workflow_request_viewed(
            self.user.id,
            self.request_id,
            self.instance.current_step_order,
        ))
        db.session.commit()
        self.assertEqual(
            unopened_workflow_request_ids(self.user.id, state),
            set(),
        )

        self.instance.current_step_order = 2
        db.session.commit()
        self.assertEqual(
            unopened_workflow_request_ids(
                self.user.id,
                {self.request_id: self.instance.current_step_order},
            ),
            {self.request_id},
        )

    def test_invalidation_makes_the_same_step_new_again(self):
        state = {self.request_id: self.instance.current_step_order}
        mark_workflow_request_viewed(
            self.user.id,
            self.request_id,
            self.instance.current_step_order,
        )
        db.session.commit()
        self.assertEqual(unopened_workflow_request_ids(self.user.id, state), set())

        self.assertEqual(invalidate_workflow_request_views(self.request_id), 1)
        db.session.commit()

        self.assertEqual(
            unopened_workflow_request_ids(self.user.id, state),
            {self.request_id},
        )

    def test_scoped_invalidation_keeps_other_users_read_state(self):
        other_user = User(
            email="other-workflow-view@example.test",
            name="Other Workflow Viewer",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        db.session.add(other_user)
        db.session.flush()
        for user_id in (self.user.id, other_user.id):
            db.session.add(WorkflowRequestView(
                user_id=user_id,
                request_id=self.request_id,
                step_order=self.instance.current_step_order,
            ))
        db.session.commit()

        self.assertEqual(
            invalidate_workflow_request_views(
                self.request_id,
                user_ids=[self.user.id],
            ),
            1,
        )
        db.session.commit()

        self.assertIsNone(WorkflowRequestView.query.filter_by(
            request_id=self.request_id,
            user_id=self.user.id,
        ).first())
        self.assertIsNotNone(WorkflowRequestView.query.filter_by(
            request_id=self.request_id,
            user_id=other_user.id,
        ).first())

    def test_admin_all_requests_marks_only_active_requests_new(self):
        excluded_requests = []
        for status in ("DRAFT", "APPROVED", "REJECTED", "CLOSED"):
            request_row = WorkflowRequest(
                requester=self.user,
                title=f"Terminal workflow request {status}",
                status=status,
            )
            db.session.add(request_row)
            db.session.flush()
            excluded_requests.append(request_row)
            db.session.add(WorkflowInstance(
                request_id=request_row.id,
                current_step_order=1,
                is_completed=True,
            ))
        db.session.commit()

        captured = {}

        def capture_template(_template_name, **context):
            captured.update(context)
            return "ok"

        route = admin_requests
        while hasattr(route, "__wrapped__"):
            route = route.__wrapped__
        with self.app.test_request_context("/admin/requests"):
            with (
                patch("admin.routes.current_user", self.user),
                patch("admin.routes.render_template", side_effect=capture_template),
            ):
                self.assertEqual(route(), "ok")

        self.assertIn(self.request_id, captured["new_request_ids"])
        self.assertTrue({row.id for row in excluded_requests}.isdisjoint(
            captured["new_request_ids"],
        ))


if __name__ == "__main__":
    unittest.main()
