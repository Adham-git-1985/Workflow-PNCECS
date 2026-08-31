import shutil
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from werkzeug.datastructures import FileStorage
from werkzeug.exceptions import Forbidden

from extensions import db
from models import Message, MessageAttachment, MessageRecipient, User
from messages import messages_bp
from messages.routes import (
    _can_access_message,
    _message_attachment_dir,
    _save_message_attachments,
    compose,
    download_attachment,
)


def _unwrapped(function):
    while hasattr(function, "__wrapped__"):
        function = function.__wrapped__
    return function


class MessageAttachmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.app = Flask(__name__, instance_path=cls.temp_dir.name)
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY="message-attachments-test",
        )
        cls.app.register_blueprint(messages_bp)
        db.init_app(cls.app)
        cls.context = cls.app.app_context()
        cls.context.push()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.context.pop()
        cls.temp_dir.cleanup()

    def setUp(self):
        db.session.remove()
        db.drop_all()
        db.create_all()
        uploads_root = Path(self.temp_dir.name) / "uploads" / "messages"
        if uploads_root.exists():
            shutil.rmtree(uploads_root)

        self.sender = User(
            email="sender@example.test",
            name="Sender",
            password_hash="not-used",
            role="EMPLOYEE",
        )
        self.recipient = User(
            email="recipient@example.test",
            name="Recipient",
            password_hash="not-used",
            role="EMPLOYEE",
        )
        self.other_user = User(
            email="other@example.test",
            name="Other",
            password_hash="not-used",
            role="EMPLOYEE",
        )
        db.session.add_all((self.sender, self.recipient, self.other_user))
        db.session.flush()

        self.message = Message(
            sender_id=self.sender.id,
            subject="مرفقات",
            body="يرجى الاطلاع",
            target_kind="USER",
            target_id=self.recipient.id,
        )
        db.session.add(self.message)
        db.session.flush()
        db.session.add(MessageRecipient(
            message_id=self.message.id,
            recipient_user_id=self.recipient.id,
        ))
        db.session.commit()

    def test_saves_multiple_attachments_and_limits_visibility_to_message_participants(self):
        uploads = [
            FileStorage(stream=BytesIO(b"pdf"), filename="قرار.pdf", content_type="application/pdf"),
            FileStorage(stream=BytesIO(b"doc"), filename="مرفق.docx", content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ]

        with self.app.test_request_context("/messages/compose"), patch(
            "messages.routes.current_user", self.sender
        ):
            count, saved_paths = _save_message_attachments(self.message, uploads)
            db.session.commit()

        attachments = MessageAttachment.query.order_by(MessageAttachment.id).all()
        self.assertEqual(count, 2)
        self.assertEqual(len(attachments), 2)
        self.assertEqual({item.original_name for item in attachments}, {"قرار.pdf", "مرفق.docx"})
        self.assertTrue(all(path.is_file() for path in saved_paths))
        self.assertTrue(all(item.stored_name != item.original_name for item in attachments))
        self.assertEqual({path.parent for path in saved_paths}, {_message_attachment_dir(self.message.id)})
        self.assertTrue(_can_access_message(self.message, self.sender))
        self.assertTrue(_can_access_message(self.message, self.recipient))
        self.assertFalse(_can_access_message(self.message, self.other_user))

    def test_oversized_attachment_is_removed_without_creating_a_record(self):
        upload = FileStorage(stream=BytesIO(b"1234"), filename="large.pdf", content_type="application/pdf")

        with self.app.test_request_context("/messages/compose"), patch(
            "messages.routes.current_user", self.sender
        ), patch("messages.routes.MESSAGE_ATTACHMENT_MAX_FILE_BYTES", 3):
            with self.assertRaisesRegex(ValueError, "يتجاوز الحد"):
                _save_message_attachments(self.message, [upload])

        self.assertEqual(MessageAttachment.query.count(), 0)
        self.assertEqual(list(_message_attachment_dir(self.message.id).glob("*")), [])

    def test_compose_accepts_an_attachment_without_message_text(self):
        compose_unwrapped = _unwrapped(compose)
        with self.app.test_request_context(
            "/messages/compose",
            method="POST",
            data={
                "target_kind": "USER",
                "target_id": str(self.recipient.id),
                "subject": "وثيقة",
                "body": "",
                "attachments": (BytesIO(b"attachment-only"), "document.pdf"),
            },
        ), patch("messages.routes.current_user", self.sender):
            response = compose_unwrapped()

        self.assertEqual(response.status_code, 302)
        sent = Message.query.filter(Message.id != self.message.id).one()
        self.assertEqual(sent.body, "")
        self.assertEqual([item.original_name for item in sent.attachments], ["document.pdf"])

    def test_download_is_available_to_a_recipient_but_not_other_users(self):
        with self.app.test_request_context("/messages/compose"), patch(
            "messages.routes.current_user", self.sender
        ):
            _save_message_attachments(
                self.message,
                [FileStorage(stream=BytesIO(b"private document"), filename="private.pdf")],
            )
            db.session.commit()

        attachment = self.message.attachments[0]
        download_unwrapped = _unwrapped(download_attachment)
        with self.app.test_request_context(f"/messages/attachment/{attachment.id}/download"), patch(
            "messages.routes.current_user", self.recipient
        ):
            response = download_unwrapped(attachment.id)
            self.assertEqual(response.status_code, 200)
            response.direct_passthrough = False
            self.assertEqual(response.get_data(), b"private document")
            response.close()

        with self.app.test_request_context(f"/messages/attachment/{attachment.id}/download"), patch(
            "messages.routes.current_user", self.other_user
        ):
            with self.assertRaises(Forbidden):
                download_unwrapped(attachment.id)


if __name__ == "__main__":
    unittest.main()
