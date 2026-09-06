import unittest
from unittest.mock import patch

from flask import Flask
from werkzeug.exceptions import Forbidden, NotFound

from extensions import db
from models import AuditLog, User, WorkflowRequest
from workflow import workflow_bp
from workflow.routes import delete_workflow_comment


def _unwrapped(function):
    while hasattr(function, "__wrapped__"):
        function = function.__wrapped__
    return function


class WorkflowCommentDeletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY="workflow-comment-deletion-test",
        )
        cls.app.register_blueprint(workflow_bp, url_prefix="/workflow")
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

        self.requester = self._user("requester@example.test", role="EMPLOYEE")
        self.admin = self._user("admin@example.test", role="ADMIN")
        self.super_admin = self._user("super-admin@example.test", role="SUPER_ADMIN")
        self.request_row = WorkflowRequest(
            requester_id=self.requester.id,
            title="طلب اختبار حذف التعليقات",
            status="IN_PROGRESS",
        )
        db.session.add(self.request_row)
        db.session.flush()
        self.comment = AuditLog(
            request_id=self.request_row.id,
            user_id=self.requester.id,
            action="WORKFLOW_COMMENT",
            note="تعليق قابل للحذف",
        )
        self.decision = AuditLog(
            request_id=self.request_row.id,
            user_id=self.admin.id,
            action="STEP_APPROVED",
            note="إجراء لا يجوز حذفه من هذه النقطة",
        )
        db.session.add_all([self.comment, self.decision])
        db.session.commit()

    @staticmethod
    def _user(email, role):
        user = User(email=email, password_hash="not-used-in-test", role=role)
        db.session.add(user)
        db.session.flush()
        return user

    def _delete(self, user, audit_log_id):
        handler = _unwrapped(delete_workflow_comment)
        with self.app.test_request_context(
            f"/workflow/request/{self.request_row.id}/comments/{audit_log_id}/delete",
            method="POST",
        ), patch("workflow.routes.current_user", user):
            return handler(self.request_row.id, audit_log_id)

    def test_super_admin_can_delete_a_workflow_comment(self):
        response = self._delete(self.super_admin, self.comment.id)

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(db.session.get(AuditLog, self.comment.id))
        deletion_audit = AuditLog.query.filter_by(
            request_id=self.request_row.id,
            action="WORKFLOW_COMMENT_DELETED",
            user_id=self.super_admin.id,
            target_id=self.comment.id,
        ).one_or_none()
        self.assertIsNotNone(deletion_audit)
        self.assertEqual(deletion_audit.note, "تم حذف تعليق من سجل المسار.")

    def test_regular_admin_cannot_delete_a_workflow_comment(self):
        with self.assertRaises(Forbidden):
            self._delete(self.admin, self.comment.id)

        self.assertIsNotNone(db.session.get(AuditLog, self.comment.id))

    def test_only_comments_and_replies_can_be_deleted(self):
        with self.assertRaises(NotFound):
            self._delete(self.super_admin, self.decision.id)

        self.assertIsNotNone(db.session.get(AuditLog, self.decision.id))


if __name__ == "__main__":
    unittest.main()
