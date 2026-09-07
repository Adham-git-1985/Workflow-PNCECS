import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from flask import Flask, g
from flask_login import LoginManager

from extensions import db
from models import CorrAttachment, InboundMail, User
from portal import portal_bp


class PortalEmlPreviewRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.app = Flask(__name__, instance_path=cls.temp_dir.name)
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="portal-eml-preview-test",
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

        cls.app.register_blueprint(portal_bp)
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

        inbound = InboundMail(
            ref_no="IN-EML-PREVIEW",
            category="GENERAL",
            sender="Test Sender",
            subject="EML preview test",
            received_date="2026-09-07",
            confidentiality="NORMAL",
            status="IN_PROGRESS",
            created_by_id=self.user.id,
        )
        db.session.add(inbound)
        db.session.flush()

        message = EmailMessage()
        message["From"] = "Sender <sender@example.test>"
        message["Subject"] = "Portal message preview"
        message.set_content("Message body")
        message.add_attachment(
            b"pdf payload",
            maintype="application",
            subtype="pdf",
            filename="report.pdf",
        )
        storage = Path(self.temp_dir.name) / "uploads" / "correspondence"
        storage.mkdir(parents=True, exist_ok=True)
        self.message_path = storage / "message.eml"
        self.message_path.write_bytes(message.as_bytes())

        attachment = CorrAttachment(
            inbound_id=inbound.id,
            original_name="message.eml",
            stored_name=self.message_path.name,
            uploaded_by_id=self.user.id,
        )
        db.session.add(attachment)
        db.session.commit()
        self.attachment_id = attachment.id

    def _login(self, client):
        g.pop("_login_user", None)
        with client.session_transaction() as session:
            session.clear()
            session["_user_id"] = str(self.user.id)
            session["_fresh"] = True

    def test_preview_lists_embedded_email_attachment_actions(self):
        with patch("portal.routes.render_template", return_value="preview") as render:
            with self.app.test_client() as client:
                self._login(client)
                response = client.get(
                    f"/portal/corr/attachment/{self.attachment_id}/eml-preview"
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(render.call_args.args[0], "portal/corr/eml_preview.html")
        embedded = render.call_args.kwargs["eml_attachments"]
        self.assertEqual(embedded[0]["filename"], "report.pdf")
        self.assertIn("/eml-attachment/1/view", embedded[0]["preview_url"])
        self.assertIn("/eml-attachment/1/download", embedded[0]["download_url"])

    def test_embedded_email_attachment_supports_preview_and_download(self):
        with self.app.test_client() as client:
            self._login(client)
            preview_response = client.get(
                f"/portal/corr/attachment/{self.attachment_id}/eml-attachment/1/view"
            )
            download_response = client.get(
                f"/portal/corr/attachment/{self.attachment_id}/eml-attachment/1/download"
            )
            missing_response = client.get(
                f"/portal/corr/attachment/{self.attachment_id}/eml-attachment/2/view"
            )

        self.assertEqual(preview_response.status_code, 200)
        self.assertEqual(preview_response.data, b"pdf payload")
        self.assertEqual(preview_response.mimetype, "application/pdf")
        self.assertIn("inline", preview_response.headers["Content-Disposition"])
        self.assertEqual(download_response.status_code, 200)
        self.assertEqual(download_response.data, b"pdf payload")
        self.assertIn("attachment", download_response.headers["Content-Disposition"])
        self.assertEqual(missing_response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
