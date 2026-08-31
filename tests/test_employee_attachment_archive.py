import tempfile
import unittest
from pathlib import Path

from flask import Flask

from archive.queries import archive_access_query
from extensions import db
from models import ArchivedFile, EmployeeAttachment, User
from services.employee_attachment_archive import (
    archive_employee_attachment_deletion,
    sync_employee_attachment_to_archive,
    sync_pending_employee_attachments_for_user,
)


class EmployeeAttachmentArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.app = Flask(__name__, instance_path=cls.temp_dir.name)
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="employee-attachment-archive-test",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            ARCHIVE_STORAGE_DIR=str(Path(cls.temp_dir.name) / "archive"),
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
        db.session.remove()
        db.drop_all()
        db.create_all()
        self.employee = User(email="employee@example.test", name="Employee", password_hash="x", role="employee")
        self.hr_user = User(email="hr@example.test", name="HR", password_hash="x", role="HR")
        db.session.add_all((self.employee, self.hr_user))
        db.session.commit()

    def _create_source_attachment(self, *, original_name="contract.pdf", contents=b"first version"):
        source_dir = Path(self.app.instance_path) / "uploads" / "employees" / str(self.employee.id)
        source_dir.mkdir(parents=True, exist_ok=True)
        stored_name = "employee-source.pdf"
        source_path = source_dir / stored_name
        source_path.write_bytes(contents)
        attachment = EmployeeAttachment(
            user_id=self.employee.id,
            attachment_type="OTHER",
            original_name=original_name,
            stored_name=stored_name,
            uploaded_by_id=self.hr_user.id,
        )
        db.session.add(attachment)
        db.session.flush()
        return attachment, source_path

    def test_sync_creates_private_archive_copy_owned_by_employee(self):
        attachment, source_path = self._create_source_attachment()

        archived = sync_employee_attachment_to_archive(attachment, source_path=source_path)
        db.session.commit()

        self.assertEqual(attachment.archived_file_id, archived.id)
        self.assertEqual(archived.owner_id, self.employee.id)
        self.assertEqual(archived.visibility, "owner")
        self.assertEqual(Path(archived.file_path).read_bytes(), b"first version")
        self.assertEqual(archive_access_query(self.employee).filter_by(id=archived.id).one(), archived)

    def test_sync_updates_the_same_archive_record_when_attachment_is_replaced(self):
        attachment, source_path = self._create_source_attachment()
        first_archive = sync_employee_attachment_to_archive(attachment, source_path=source_path)
        db.session.commit()

        source_path.write_bytes(b"replaced version")
        attachment.original_name = "replacement.pdf"
        updated_archive = sync_employee_attachment_to_archive(attachment, source_path=source_path)
        db.session.commit()

        self.assertEqual(updated_archive.id, first_archive.id)
        self.assertEqual(updated_archive.original_name, "replacement.pdf")
        self.assertEqual(Path(updated_archive.file_path).read_bytes(), b"replaced version")
        self.assertEqual(ArchivedFile.query.count(), 1)

    def test_legacy_attachment_is_backfilled_once_for_its_employee(self):
        attachment, _ = self._create_source_attachment(contents=b"legacy attachment")
        db.session.commit()

        self.assertEqual(sync_pending_employee_attachments_for_user(self.employee.id), 1)
        self.assertIsNotNone(attachment.archived_file_id)
        self.assertEqual(sync_pending_employee_attachments_for_user(self.employee.id), 0)

    def test_deleting_employee_attachment_hides_its_archive_copy(self):
        attachment, source_path = self._create_source_attachment()
        archived = sync_employee_attachment_to_archive(attachment, source_path=source_path)
        db.session.commit()

        archive_employee_attachment_deletion(attachment, deleted_by_id=self.hr_user.id)
        db.session.commit()

        self.assertTrue(archived.is_deleted)
        self.assertEqual(archived.deleted_by, self.hr_user.id)
        self.assertIsNone(archive_access_query(self.employee).filter_by(id=archived.id).first())


if __name__ == "__main__":
    unittest.main()
