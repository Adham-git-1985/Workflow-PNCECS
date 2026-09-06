import unittest

from flask import Flask

from extensions import db
from models import (
    AuditLog,
    Notification,
    User,
    WorkflowInstance,
    WorkflowInstanceStep,
    WorkflowRequest,
    WorkflowStepTask,
)
from workflow.engine import reopen_workflow_to_step


class WorkflowReopenToStepTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY="test-only",
        )
        db.init_app(cls.app)
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

        self.actor = self._user("superadmin@example.test", "SUPER_ADMIN")
        self.first_approver = self._user("first@example.test", "EMPLOYEE")
        self.second_approver = self._user("second@example.test", "EMPLOYEE")
        self.request_row = WorkflowRequest(
            title="طلب لإعادة الفتح",
            status="CLOSED",
            requester_id=self.first_approver.id,
            confidentiality="NORMAL",
        )
        db.session.add(self.request_row)
        db.session.flush()
        self.instance = WorkflowInstance(
            request_id=self.request_row.id,
            current_step_order=2,
            is_completed=True,
            last_step_actor_id=self.second_approver.id,
        )
        db.session.add(self.instance)
        db.session.flush()
        self.first_step = WorkflowInstanceStep(
            instance_id=self.instance.id,
            step_order=1,
            mode="SEQUENTIAL",
            approver_kind="USER",
            approver_user_id=self.first_approver.id,
            status="APPROVED",
            decided_by_id=self.first_approver.id,
        )
        self.second_step = WorkflowInstanceStep(
            instance_id=self.instance.id,
            step_order=2,
            mode="PARALLEL_SYNC",
            approver_kind="USER",
            approver_user_id=self.second_approver.id,
            status="APPROVED",
            decided_by_id=self.second_approver.id,
            note="قرار سابق",
        )
        db.session.add_all((self.first_step, self.second_step))
        db.session.flush()
        db.session.add(WorkflowStepTask(
            instance_id=self.instance.id,
            request_id=self.request_row.id,
            step_order=2,
            assignee_user_id=self.second_approver.id,
            status="RESPONDED",
            response="APPROVE",
        ))
        db.session.commit()

    @staticmethod
    def _user(email, role):
        user = User(email=email, password_hash="not-used-in-test", role=role)
        db.session.add(user)
        db.session.flush()
        return user

    def test_reopen_resets_selected_step_and_preserves_prior_history(self):
        reopened_step = reopen_workflow_to_step(
            self.request_row.id,
            2,
            self.actor.id,
            "يلزم استكمال المراجعة.",
        )
        db.session.commit()

        db.session.refresh(self.request_row)
        db.session.refresh(self.instance)
        db.session.refresh(self.first_step)
        db.session.refresh(self.second_step)

        self.assertEqual(reopened_step.id, self.second_step.id)
        self.assertEqual(self.request_row.status, "IN_PROGRESS")
        self.assertFalse(self.instance.is_completed)
        self.assertEqual(self.instance.current_step_order, 2)
        self.assertEqual(self.instance.last_step_actor_id, self.actor.id)
        self.assertEqual(self.first_step.status, "APPROVED")
        self.assertEqual(self.first_step.decided_by_id, self.first_approver.id)
        self.assertEqual(self.second_step.status, "PENDING")
        self.assertIsNone(self.second_step.decided_by_id)
        self.assertIsNone(self.second_step.note)
        self.assertIsNotNone(self.second_step.due_at)
        self.assertEqual(WorkflowStepTask.query.count(), 0)
        self.assertIsNotNone(AuditLog.query.filter_by(
            request_id=self.request_row.id,
            action="WORKFLOW_REOPENED_TO_STEP",
            target_id=self.second_step.id,
        ).first())

    def test_reopen_rejects_a_step_after_the_current_step(self):
        with self.assertRaisesRegex(ValueError, "خطوة حالية أو سابقة"):
            reopen_workflow_to_step(
                self.request_row.id,
                3,
                self.actor.id,
                "محاولة غير صالحة.",
            )
        db.session.rollback()
        self.assertEqual(Notification.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
