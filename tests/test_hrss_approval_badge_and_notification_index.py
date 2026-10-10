import unittest

from flask import Flask
from sqlalchemy import inspect

from extensions import db
from models import (
    HRSSRequest,
    HRSSRequestApproval,
    Notification,
    User,
    UserPermission,
)
from portal.routes import _approval_inbox_pending_count


class HRSSApprovalBadgeAndNotificationIndexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY="hrss-approval-badge-test",
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

    @staticmethod
    def _request(requester, status="IN_REVIEW"):
        request_row = HRSSRequest(
            requester_id=requester.id,
            type_code="PROFILE_UPDATE",
            status=status,
            current_step_no=1 if status == "IN_REVIEW" else None,
        )
        db.session.add(request_row)
        db.session.flush()
        return request_row

    @staticmethod
    def _approval(request_row, *, user_id=None, role=None, status="PENDING"):
        db.session.add(HRSSRequestApproval(
            request_id=request_row.id,
            step_no=1,
            approver_user_id=user_id,
            approver_role=role,
            status=status,
        ))

    def test_hrss_badge_counts_only_active_assignments_for_regular_approver(self):
        approver = self._user("hrss-approver@example.test", role="TEAM_MANAGER")
        requester = self._user("hrss-requester@example.test")
        other_approver = self._user("hrss-other-approver@example.test")
        db.session.add(UserPermission(
            user_id=approver.id,
            key="HR_SS_APPROVE",
            is_allowed=True,
        ))

        assigned_by_user = self._request(requester)
        assigned_by_role = self._request(requester)
        assigned_elsewhere = self._request(requester)
        closed_with_pending_step = self._request(requester, status="APPROVED")
        self._approval(assigned_by_user, user_id=approver.id)
        self._approval(assigned_by_role, role="team-manager")
        self._approval(assigned_elsewhere, user_id=other_approver.id)
        self._approval(closed_with_pending_step, user_id=approver.id)
        db.session.commit()

        with self.app.test_request_context("/"):
            selected_user = db.session.get(User, approver.id)
            self.assertEqual(_approval_inbox_pending_count(selected_user), 2)

    def test_hrss_badge_global_manager_counts_all_active_pending_steps(self):
        manager = self._user("hrss-manager@example.test", role="HR_MANAGER")
        requester = self._user("hrss-global-requester@example.test")
        other_approver = self._user("hrss-global-other@example.test")
        db.session.add(UserPermission(
            user_id=manager.id,
            key="HR_SS_WORKFLOWS_MANAGE",
            is_allowed=True,
        ))

        first = self._request(requester)
        second = self._request(requester)
        closed = self._request(requester, status="REJECTED")
        self._approval(first, user_id=other_approver.id)
        self._approval(second, role="UNRELATED_ROLE")
        self._approval(closed, user_id=other_approver.id)
        db.session.commit()

        with self.app.test_request_context("/"):
            selected_user = db.session.get(User, manager.id)
            self.assertEqual(_approval_inbox_pending_count(selected_user), 2)

    def test_followup_reminder_unique_index_exists_on_fresh_schema(self):
        model_index = next(
            index
            for index in Notification.__table__.indexes
            if index.name == "uq_notification_followup_reminder_event"
        )
        self.assertTrue(model_index.unique)
        self.assertEqual(
            [column.name for column in model_index.columns],
            ["user_id", "event_key", "is_mirror"],
        )

        database_indexes = {
            index["name"]: index
            for index in inspect(db.engine).get_indexes("notification")
        }
        self.assertIn("uq_notification_followup_reminder_event", database_indexes)
        self.assertTrue(
            database_indexes["uq_notification_followup_reminder_event"]["unique"]
        )


if __name__ == "__main__":
    unittest.main()
