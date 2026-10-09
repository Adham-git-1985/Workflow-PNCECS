import unittest

from flask import Flask

from extensions import db
from models import User, WorkflowInstance, WorkflowRequest
from services.workflow_request_views import (
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


if __name__ == "__main__":
    unittest.main()
