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
    WorkflowTemplate,
    WorkflowTemplateParallelAssignee,
    WorkflowTemplateStep,
)
from workflow.engine import (
    authorize_parallel_step,
    decide_step,
    ensure_parallel_tasks,
    start_workflow_for_request,
)
from workflow.routes import _user_can_view_request


class ParallelStepAuthorizationTests(unittest.TestCase):
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

        self.actor = self._user("actor@example.test")
        self.selected = self._user("selected@example.test")
        self.excluded = self._user("excluded@example.test")
        self.outsider = self._user("outsider@example.test")

        self.template = WorkflowTemplate(
            name="مسار باختيار متزامن",
            created_by_id=self.actor.id,
        )
        db.session.add(self.template)
        db.session.flush()

        self.template_step_1 = WorkflowTemplateStep(
            template_id=self.template.id,
            step_order=1,
            mode="SEQUENTIAL",
            approver_kind="USER",
            approver_user_id=self.actor.id,
        )
        self.template_step_2 = WorkflowTemplateStep(
            template_id=self.template.id,
            step_order=2,
            mode="PARALLEL_SYNC",
            approver_kind="USER",
            approver_user_id=self.selected.id,
        )
        db.session.add_all([self.template_step_1, self.template_step_2])
        db.session.flush()
        db.session.add(WorkflowTemplateParallelAssignee(
            template_step_id=self.template_step_2.id,
            template_id=self.template.id,
            step_order=2,
            approver_kind="USER",
            approver_user_id=self.excluded.id,
        ))

        self.request = WorkflowRequest(
            title="طلب تجريبي",
            status="IN_PROGRESS",
            requester_id=self.actor.id,
            confidentiality="NORMAL",
        )
        db.session.add(self.request)
        db.session.flush()
        self.instance = WorkflowInstance(
            request_id=self.request.id,
            template_id=self.template.id,
            current_step_order=1,
            last_step_actor_id=self.actor.id,
        )
        db.session.add(self.instance)
        db.session.flush()
        self.step_1 = WorkflowInstanceStep(
            instance_id=self.instance.id,
            step_order=1,
            mode="SEQUENTIAL",
            approver_kind="USER",
            approver_user_id=self.actor.id,
            status="PENDING",
        )
        self.step_2 = WorkflowInstanceStep(
            instance_id=self.instance.id,
            step_order=2,
            mode="PARALLEL_SYNC",
            approver_kind="USER",
            approver_user_id=self.selected.id,
            status="PENDING",
        )
        db.session.add_all([self.step_1, self.step_2])
        db.session.commit()

    @staticmethod
    def _user(email):
        user = User(
            email=email,
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        db.session.add(user)
        db.session.flush()
        return user

    def test_template_candidates_do_not_receive_tasks_without_authorization(self):
        self.instance.current_step_order = 2
        db.session.commit()

        ensure_parallel_tasks(self.request, self.instance, self.step_2)
        db.session.commit()

        self.assertEqual(WorkflowStepTask.query.count(), 0)
        self.assertEqual(Notification.query.count(), 0)
        self.assertFalse(_user_can_view_request(self.selected, self.request))
        self.assertFalse(_user_can_view_request(self.excluded, self.request))

    def test_only_selected_candidates_receive_access_and_notification(self):
        self.instance.current_step_order = 2
        db.session.commit()

        authorize_parallel_step(
            self.request,
            self.instance,
            self.step_2,
            authorized_user_ids=[self.selected.id],
            actor_user_id=self.actor.id,
            effective_user_id=self.actor.id,
        )
        db.session.commit()

        task_user_ids = {
            row.assignee_user_id for row in WorkflowStepTask.query.all()
        }
        notification_user_ids = {
            row.user_id
            for row in Notification.query.filter_by(is_mirror=False).all()
        }
        self.assertEqual(task_user_ids, {self.selected.id})
        self.assertEqual(notification_user_ids, {self.selected.id})
        self.assertTrue(_user_can_view_request(self.selected, self.request))
        self.assertFalse(_user_can_view_request(self.excluded, self.request))
        self.assertFalse(_user_can_view_request(self.outsider, self.request))
        self.assertIsNotNone(AuditLog.query.filter_by(
            action="PARALLEL_SYNC_AUTHORIZED",
            target_id=self.step_2.id,
        ).first())

    def test_sequential_approval_requires_selection_for_next_parallel_step(self):
        with self.assertRaisesRegex(ValueError, "اختر شخصًا واحدًا"):
            decide_step(
                self.request.id,
                self.step_1.step_order,
                self.actor.id,
                "APPROVED",
                effective_user_id=self.actor.id,
            )
        db.session.rollback()

        stored_step = WorkflowInstanceStep.query.get(self.step_1.id)
        self.assertEqual(stored_step.status, "PENDING")
        self.assertEqual(WorkflowStepTask.query.count(), 0)

    def test_sequential_approval_activates_only_selected_participants(self):
        decide_step(
            self.request.id,
            self.step_1.step_order,
            self.actor.id,
            "APPROVED",
            effective_user_id=self.actor.id,
            authorized_parallel_user_ids=[self.selected.id],
        )
        db.session.commit()

        instance = WorkflowInstance.query.get(self.instance.id)
        self.assertEqual(instance.current_step_order, 2)
        self.assertEqual(
            {task.assignee_user_id for task in WorkflowStepTask.query.all()},
            {self.selected.id},
        )
        self.assertTrue(_user_can_view_request(self.selected, self.request))
        self.assertFalse(_user_can_view_request(self.excluded, self.request))

    def test_non_candidate_cannot_be_authorized(self):
        self.instance.current_step_order = 2
        db.session.commit()

        with self.assertRaisesRegex(ValueError, "المرشحين"):
            authorize_parallel_step(
                self.request,
                self.instance,
                self.step_2,
                authorized_user_ids=[self.outsider.id],
                actor_user_id=self.actor.id,
                effective_user_id=self.actor.id,
            )
        db.session.rollback()
        self.assertEqual(WorkflowStepTask.query.count(), 0)

    def test_parallel_candidate_cannot_authorize_the_step_for_themselves(self):
        self.instance.current_step_order = 2
        db.session.commit()

        with self.assertRaisesRegex(PermissionError, "غير مخوّل"):
            authorize_parallel_step(
                self.request,
                self.instance,
                self.step_2,
                authorized_user_ids=[self.selected.id],
                actor_user_id=self.selected.id,
                effective_user_id=self.selected.id,
            )
        db.session.rollback()
        self.assertEqual(WorkflowStepTask.query.count(), 0)

    def test_workflow_can_start_with_only_selected_first_parallel_participants(self):
        template = WorkflowTemplate(
            name="مسار صادر يبدأ بالتزامن",
            created_by_id=self.actor.id,
        )
        db.session.add(template)
        db.session.flush()
        first_step = WorkflowTemplateStep(
            template_id=template.id,
            step_order=1,
            mode="PARALLEL_SYNC",
            approver_kind="USER",
            approver_user_id=self.selected.id,
        )
        db.session.add(first_step)
        db.session.flush()
        db.session.add(WorkflowTemplateParallelAssignee(
            template_step_id=first_step.id,
            template_id=template.id,
            step_order=1,
            approver_kind="USER",
            approver_user_id=self.excluded.id,
        ))
        request = WorkflowRequest(
            title="صادر تجريبي",
            status="DRAFT",
            requester_id=self.actor.id,
            confidentiality="NORMAL",
        )
        db.session.add(request)
        db.session.flush()

        start_workflow_for_request(
            request,
            template,
            created_by_user_id=self.actor.id,
            auto_commit=False,
            initial_parallel_user_ids=[self.selected.id],
        )
        db.session.commit()

        instance = WorkflowInstance.query.filter_by(request_id=request.id).one()
        task_user_ids = {
            task.assignee_user_id
            for task in WorkflowStepTask.query.filter_by(instance_id=instance.id).all()
        }
        self.assertEqual(task_user_ids, {self.selected.id})
        self.assertFalse(_user_can_view_request(self.excluded, request))


if __name__ == "__main__":
    unittest.main()
