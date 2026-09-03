import shutil
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from werkzeug.datastructures import FileStorage

from extensions import db
from models import EmployeeFile, PortalCircular, PortalCircularAttachment, User
from portal.routes import (
    _circular_whatsapp_text,
    _circular_user_emails,
    _remove_circular_attachment_files,
    _save_circular_attachments,
    _send_circular_to_email,
)


class CircularAttachmentStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.app = Flask(__name__, instance_path=cls.temp_dir.name)
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY="test-only",
        )
        db.init_app(cls.app)
        cls.context = cls.app.app_context()
        cls.context.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.context.pop()
        cls.temp_dir.cleanup()

    def setUp(self):
        db.session.query(PortalCircularAttachment).delete()
        db.session.query(PortalCircular).delete()
        db.session.commit()
        upload_root = Path(self.temp_dir.name) / "uploads" / "circulars"
        if upload_root.exists():
            shutil.rmtree(upload_root)

    def _circular(self):
        row = PortalCircular(
            title="تعميم بمرفقات",
            body="النص",
            target_scope="ALL",
        )
        db.session.add(row)
        db.session.commit()
        return row

    def test_saves_multiple_files_with_random_storage_names(self):
        row = self._circular()
        uploads = [
            FileStorage(stream=BytesIO(b"first"), filename="قرار.pdf", content_type="application/pdf"),
            FileStorage(stream=BytesIO(b"second"), filename="صورة المسح.jpg", content_type="image/jpeg"),
        ]

        with self.app.test_request_context("/"):
            count, saved_paths = _save_circular_attachments(row, uploads)
            circular_text = _circular_whatsapp_text(row)
            db.session.commit()

        attachments = PortalCircularAttachment.query.order_by(PortalCircularAttachment.id).all()
        self.assertEqual(count, 2)
        self.assertEqual(len(attachments), 2)
        self.assertEqual({item.original_name for item in attachments}, {"قرار.pdf", "صورة المسح.jpg"})
        self.assertIn("المرفقات: 2", circular_text)
        self.assertTrue(all(path.exists() for path in saved_paths))
        self.assertTrue(all(item.stored_name != item.original_name for item in attachments))
        self.assertTrue(all(Path(path).parent.name == str(row.id) for path in saved_paths))

    def test_oversized_file_is_rejected_and_removed(self):
        row = self._circular()
        upload = FileStorage(
            stream=BytesIO(b"1234"),
            filename="كبير.pdf",
            content_type="application/pdf",
        )

        with self.app.test_request_context("/"), patch(
            "portal.routes.CIRCULAR_ATTACHMENT_MAX_FILE_BYTES",
            3,
        ):
            with self.assertRaisesRegex(ValueError, "يتجاوز الحد"):
                _save_circular_attachments(row, [upload])
            db.session.rollback()

        attachment_dir = Path(self.temp_dir.name) / "uploads" / "circulars" / str(row.id)
        self.assertEqual(list(attachment_dir.glob("*")), [])
        self.assertEqual(PortalCircularAttachment.query.count(), 0)

    def test_email_contains_saved_circular_attachments(self):
        row = self._circular()
        upload = FileStorage(
            stream=BytesIO(b"pdf-content"),
            filename="مرفق التعميم.pdf",
            content_type="application/pdf",
        )
        with self.app.test_request_context("/"):
            _save_circular_attachments(row, [upload])
            db.session.commit()
            db.session.expire(row, ["attachments"])

            email_config = {
                "enabled": True,
                "ready": True,
                "host": "smtp.example.test",
                "port": 25,
                "security": "none",
                "username": "",
                "password": "",
                "from_email": "portal@example.test",
                "from_name": "البوابة",
                "reply_to": "",
                "batch_size": 50,
            }
            with patch("portal.routes._email_circular_settings", return_value=email_config), patch(
                "portal.routes._circular_user_emails",
                return_value=["recipient@example.test"],
            ), patch("smtplib.SMTP") as smtp_class:
                category, _ = _send_circular_to_email(row)

        self.assertEqual(category, "success")
        sent_message = smtp_class.return_value.send_message.call_args.args[0]
        attachments = list(sent_message.iter_attachments())
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].get_filename(), "مرفق التعميم.pdf")
        self.assertEqual(attachments[0].get_payload(decode=True), b"pdf-content")

    def test_circular_uses_the_email_from_the_employee_file(self):
        user = User(
            email="legacy-account@example.test",
            name="Employee",
            password_hash="unused",
            role="EMPLOYEE",
        )
        db.session.add(user)
        db.session.flush()
        db.session.add(EmployeeFile(
            user_id=user.id,
            email="official-employee@example.test",
        ))
        db.session.commit()

        self.assertEqual(
            _circular_user_emails([user.id]),
            ["official-employee@example.test"],
        )

    def test_registered_attachment_files_are_removed_with_circular(self):
        row = self._circular()
        upload = FileStorage(
            stream=BytesIO(b"to-delete"),
            filename="للحذف.pdf",
            content_type="application/pdf",
        )
        with self.app.test_request_context("/"):
            _, saved_paths = _save_circular_attachments(row, [upload])
            db.session.commit()
            stored_names = [attachment.stored_name for attachment in row.attachments]
            removed = _remove_circular_attachment_files(row.id, stored_names)

        self.assertEqual(removed, 1)
        self.assertFalse(saved_paths[0].exists())
        self.assertFalse(saved_paths[0].parent.exists())


if __name__ == "__main__":
    unittest.main()
