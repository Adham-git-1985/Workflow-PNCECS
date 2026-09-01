import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from flask_login import LoginManager

from extensions import db
from models import ArchivedFile, RequestAttachment, User, WorkflowRequest
from workflow import workflow_bp


class WorkflowEmlPreviewRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.app = Flask(__name__)
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="workflow-eml-preview-test",
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
        cls.context.pop()
        cls.temp_dir.cleanup()

    def setUp(self):
        db.session.remove()
        db.drop_all()
        db.create_all()

        self.user = User(
            email="super-admin@example.test",
            name="Super Admin",
            password_hash="not-used-in-test",
            role="SUPER_ADMIN",
        )
        db.session.add(self.user)
        db.session.flush()

        request = WorkflowRequest(
            title="EML preview test",
            status="IN_PROGRESS",
            requester_id=self.user.id,
            confidentiality="NORMAL",
        )
        db.session.add(request)
        db.session.flush()

        message = EmailMessage()
        message["From"] = "Sender <sender@example.test>"
        message["Subject"] = "Safe message preview"
        message.set_content("Plain preview body")
        message.add_attachment(
            b"image bytes",
            maintype="image",
            subtype="jpeg",
            filename="photo.jpg",
        )
        self.message_path = Path(self.temp_dir.name) / "message.eml"
        self.message_path.write_bytes(message.as_bytes())

        attachment = ArchivedFile(
            original_name="message.eml",
            stored_name="message.eml",
            file_path=str(self.message_path),
            mime_type="message/rfc822",
            file_size=self.message_path.stat().st_size,
            owner_id=self.user.id,
        )
        db.session.add(attachment)
        db.session.flush()
        db.session.add(RequestAttachment(
            request_id=request.id,
            archived_file_id=attachment.id,
        ))
        db.session.commit()
        self.attachment_id = attachment.id

    def _login(self, client):
        with client.session_transaction() as session:
            session["_user_id"] = str(self.user.id)
            session["_fresh"] = True

    def test_eml_attachment_uses_safe_text_preview(self):
        with patch("workflow.routes.render_template", return_value="preview") as render:
            with self.app.test_client() as client:
                self._login(client)
                response = client.get(f"/workflow/attachment/{self.attachment_id}/preview")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), "preview")
        self.assertEqual(render.call_args.args[0], "workflow/eml_preview.html")
        preview = render.call_args.kwargs["preview"]
        self.assertEqual(preview.subject, "Safe message preview")
        self.assertIn("Plain preview body", preview.body)
        self.assertEqual(preview.attachments[0].filename, "photo.jpg")


if __name__ == "__main__":
    unittest.main()
