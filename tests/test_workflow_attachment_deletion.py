import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from flask_login import LoginManager
from werkzeug.exceptions import Forbidden

from extensions import db
from models import ArchivedFile, AuditLog, RequestAttachment, User, WorkflowRequest
from workflow import workflow_bp
from workflow.routes import delete_workflow_attachment


def _unwrapped(function):
    while hasattr(function, "__wrapped__"):
        function = function.__wrapped__
    return function


class WorkflowAttachmentDeletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.app = Flask(__name__, instance_path=cls.temp_dir.name)
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="workflow-attachment-deletion-test",
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

        cls.app.register_blueprint(workflow_bp, url_prefix="/workflow")
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

        self.requester = User(
            email="requester@example.test",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        self.outsider = User(
            email="outsider@example.test",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        db.session.add_all((self.requester, self.outsider))
        db.session.commit()

        self.request_row = WorkflowRequest(
            requester_id=self.requester.id,
            title="Workflow attachment deletion test",
            status="IN_PROGRESS",
        )
        db.session.add(self.request_row)
        db.session.flush()

        self.file_path = Path(self.temp_dir.name) / "workflow-attachment.txt"
        self.file_path.write_bytes(b"workflow attachment")
        self.file = ArchivedFile(
            original_name="workflow-attachment.txt",
            stored_name="workflow-attachment.txt",
            file_path=str(self.file_path),
            file_size=self.file_path.stat().st_size,
            owner_id=self.requester.id,
            visibility="workflow",
            is_deleted=False,
        )
        db.session.add(self.file)
        db.session.flush()

        self.attachment = RequestAttachment(
            request_id=self.request_row.id,
            archived_file_id=self.file.id,
        )
        db.session.add(self.attachment)
        db.session.commit()

    def _delete(self, user):
        handler = _unwrapped(delete_workflow_attachment)
        with self.app.test_request_context(
            f"/workflow/request/{self.request_row.id}/attachments/{self.attachment.id}/delete",
            method="POST",
        ), patch("workflow.routes.current_user", user):
            return handler(self.request_row.id, self.attachment.id)

    def test_requester_removes_link_and_moves_archive_file_to_recycle_bin(self):
        response = self._delete(self.requester)

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(db.session.get(RequestAttachment, self.attachment.id))
        archived_file = db.session.get(ArchivedFile, self.file.id)
        self.assertIsNotNone(archived_file)
        self.assertTrue(archived_file.is_deleted)
        self.assertEqual(archived_file.deleted_by, self.requester.id)
        self.assertTrue(self.file_path.exists())
        audit = AuditLog.query.filter_by(
            request_id=self.request_row.id,
            action="WORKFLOW_ATTACHMENT_DELETED",
            target_id=self.file.id,
        ).one_or_none()
        self.assertIsNotNone(audit)
        self.assertIn("removed_from_archive=True", audit.note)

    def test_file_linked_to_another_workflow_stays_in_archive(self):
        other_request = WorkflowRequest(
            requester_id=self.outsider.id,
            title="Another workflow",
            status="IN_PROGRESS",
        )
        db.session.add(other_request)
        db.session.flush()
        other_attachment = RequestAttachment(
            request_id=other_request.id,
            archived_file_id=self.file.id,
        )
        db.session.add(other_attachment)
        db.session.commit()

        response = self._delete(self.requester)

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(db.session.get(RequestAttachment, self.attachment.id))
        self.assertIsNotNone(db.session.get(RequestAttachment, other_attachment.id))
        self.assertFalse(db.session.get(ArchivedFile, self.file.id).is_deleted)
        self.assertTrue(self.file_path.exists())
        audit = AuditLog.query.filter_by(
            request_id=self.request_row.id,
            action="WORKFLOW_ATTACHMENT_DELETED",
            target_id=self.file.id,
        ).one()
        self.assertIn("removed_from_archive=False", audit.note)

    def test_unrelated_user_cannot_remove_attachment(self):
        with self.assertRaises(Forbidden):
            self._delete(self.outsider)

        self.assertIsNotNone(db.session.get(RequestAttachment, self.attachment.id))
        self.assertTrue(self.file_path.exists())


if __name__ == "__main__":
    unittest.main()
