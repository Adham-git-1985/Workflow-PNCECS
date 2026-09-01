import unittest

from flask import Flask

from extensions import db
from models import (
    AuditLog,
    Committee,
    CommitteeAssignee,
    User,
    WorkflowInstance,
    WorkflowInstanceStep,
    WorkflowRequest,
    WorkflowStepTask,
)
from workflow.engine import (
    bypass_all_parallel_tasks,
    bypass_parallel_task,
    can_committee_chair_bypass_parallel_step,
)


class CommitteeChairParallelBypassTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY="committee-chair-bypass-test",
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
        db.session.remove()
        db.drop_all()
        db.create_all()

        self.requester = self._user("requester@example.test")
        self.previous_actor = self._user("previous@example.test")
        self.chair = self._user("chair@example.test")
        self.member_one = self._user("member-one@example.test")
        self.member_two = self._user("member-two@example.test")
        self.outsider = self._user("outsider@example.test")

        committee = Committee(name_ar="لجنة الاختبار", is_active=True)
        db.session.add(committee)
        db.session.flush()
        db.session.add_all([
            CommitteeAssignee(
                committee_id=committee.id,
                kind="USER",
                user_id=self.chair.id,
                member_role="CHAIR",
                is_active=True,
            ),
            CommitteeAssignee(
                committee_id=committee.id,
                kind="USER",
                user_id=self.member_one.id,
                member_role="MEMBER",
                is_active=True,
            ),
            CommitteeAssignee(
                committee_id=committee.id,
                kind="USER",
                user_id=self.member_two.id,
                member_role="MEMBER",
                is_active=True,
            ),
        ])

        self.request = WorkflowRequest(
            requester_id=self.requester.id,
            title="Committee parallel request",
            status="IN_PROGRESS",
            confidentiality="NORMAL",
        )
        db.session.add(self.request)
        db.session.flush()
        self.instance = WorkflowInstance(
            request_id=self.request.id,
            current_step_order=1,
            last_step_actor_id=self.previous_actor.id,
        )
        db.session.add(self.instance)
        db.session.flush()
        self.step = WorkflowInstanceStep(
            instance_id=self.instance.id,
            step_order=1,
            mode="PARALLEL_SYNC",
            approver_kind="COMMITTEE",
            approver_committee_id=committee.id,
            committee_delivery_mode="Committee_ALL",
            status="PENDING",
        )
        db.session.add(self.step)
        db.session.flush()
        db.session.add_all([
            WorkflowStepTask(
                instance_id=self.instance.id,
                request_id=self.request.id,
                step_order=1,
                assignee_user_id=user.id,
                status="PENDING",
                response="NONE",
            )
            for user in (self.chair, self.member_one, self.member_two)
        ])
        db.session.commit()

    @staticmethod
    def _user(email):
        user = User(email=email, password_hash="not-used-in-test", role="EMPLOYEE")
        db.session.add(user)
        db.session.flush()
        return user

    def _task(self, user):
        return WorkflowStepTask.query.filter_by(
            instance_id=self.instance.id,
            step_order=1,
            assignee_user_id=user.id,
        ).one()

    def test_chair_can_bypass_another_member_but_not_own_task(self):
        self.assertTrue(
            can_committee_chair_bypass_parallel_step(self.chair.id, self.step)
        )
        self.assertFalse(
            can_committee_chair_bypass_parallel_step(self.member_one.id, self.step)
        )

        bypass_parallel_task(
            self.request.id,
            1,
            actor_user_id=self.chair.id,
            effective_user_id=self.chair.id,
            assignee_user_id=self.member_one.id,
            reason="Member unavailable",
        )

        self.assertEqual(self._task(self.member_one).status, "BYPASSED")
        self.assertEqual(self._task(self.chair).status, "PENDING")
        self.assertEqual(
            AuditLog.query.filter_by(action="PARALLEL_SYNC_BYPASS").count(),
            1,
        )

        with self.assertRaises(PermissionError):
            bypass_parallel_task(
                self.request.id,
                1,
                actor_user_id=self.chair.id,
                effective_user_id=self.chair.id,
                assignee_user_id=self.chair.id,
                reason="Should not be allowed",
            )

    def test_chair_can_bypass_remaining_members_without_bypassing_self(self):
        bypass_all_parallel_tasks(
            self.request.id,
            1,
            actor_user_id=self.chair.id,
            effective_user_id=self.chair.id,
            reason="Members unavailable",
        )

        self.assertEqual(self._task(self.member_one).status, "BYPASSED")
        self.assertEqual(self._task(self.member_two).status, "BYPASSED")
        self.assertEqual(self._task(self.chair).status, "PENDING")
        self.assertEqual(self.step.status, "PENDING")
        self.assertEqual(self.instance.current_step_order, 1)

    def test_non_chair_cannot_bypass_committee_members(self):
        with self.assertRaises(PermissionError):
            bypass_parallel_task(
                self.request.id,
                1,
                actor_user_id=self.outsider.id,
                effective_user_id=self.outsider.id,
                assignee_user_id=self.member_one.id,
                reason="Not authorized",
            )

    def test_chair_authority_does_not_apply_to_chair_only_delivery(self):
        self.step.committee_delivery_mode = "Committee_CHAIR"
        db.session.commit()

        self.assertFalse(
            can_committee_chair_bypass_parallel_step(self.chair.id, self.step)
        )
        with self.assertRaises(PermissionError):
            bypass_parallel_task(
                self.request.id,
                1,
                actor_user_id=self.chair.id,
                effective_user_id=self.chair.id,
                assignee_user_id=self.member_one.id,
                reason="Wrong delivery mode",
            )


if __name__ == "__main__":
    unittest.main()
