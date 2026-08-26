import tempfile
import unittest

from flask import Flask, g
from flask_login import LoginManager

from archive import archive_bp
from extensions import db
from models import ArchivedFile, AuditLog, FilePermission, User


class ArchiveDeletionRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.app = Flask(__name__, instance_path=cls.temp_dir.name)
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="archive-deletion-test",
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

        cls.app.register_blueprint(archive_bp)
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

        self.super_admin = User(
            email="super-admin@example.test",
            name="Super Admin",
            password_hash="not-used-in-test",
            role="SUPER_ADMIN",
        )
        self.owner = User(
            email="owner@example.test",
            name="Owner",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        self.recipient = User(
            email="recipient@example.test",
            name="Recipient",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        self.admin = User(
            email="admin@example.test",
            name="Admin",
            password_hash="not-used-in-test",
            role="ADMIN",
        )
        db.session.add_all((self.super_admin, self.owner, self.recipient, self.admin))
        db.session.commit()

    def _login(self, client, user_id):
        g.pop("_login_user", None)
        with client.session_transaction() as session:
            session.clear()
            session["_user_id"] = str(user_id)
            session["_fresh"] = True

    def _archive_file(self, *, signed=False, shared=False):
        archived_file = ArchivedFile(
            original_name="archive-test.pdf",
            stored_name="archive-test.pdf",
            file_path="storage/archive/archive-test.pdf",
            mime_type="application/pdf",
            owner_id=self.owner.id,
            is_signed=signed,
        )
        db.session.add(archived_file)
        db.session.flush()

        if shared:
            db.session.add(FilePermission(
                file_id=archived_file.id,
                user_id=self.recipient.id,
                shared_by=self.owner.id,
            ))

        db.session.commit()
        return archived_file.id

    def test_super_admin_can_delete_signed_and_shared_file(self):
        file_id = self._archive_file(signed=True, shared=True)

        with self.app.test_client() as client:
            self._login(client, self.super_admin.id)
            response = client.post(f"/archive/delete/{file_id}")

        self.assertEqual(response.status_code, 302)
        archived_file = db.session.get(ArchivedFile, file_id)
        self.assertTrue(
            archived_file.is_deleted,
            response.headers.get("Location"),
        )
        self.assertEqual(archived_file.deleted_by, self.super_admin.id)
        self.assertEqual(FilePermission.query.filter_by(file_id=file_id).count(), 1)
        self.assertIsNotNone(AuditLog.query.filter_by(
            action="ARCHIVE_DELETE",
            user_id=self.super_admin.id,
            target_id=file_id,
        ).first())

    def test_regular_admin_still_cannot_delete_signed_file(self):
        file_id = self._archive_file(signed=True)

        with self.app.test_client() as client:
            self._login(client, self.admin.id)
            response = client.post(f"/archive/delete/{file_id}")

        self.assertEqual(response.status_code, 302)
        self.assertFalse(db.session.get(ArchivedFile, file_id).is_deleted)

    def test_owner_still_cannot_delete_shared_file(self):
        file_id = self._archive_file(shared=True)

        with self.app.test_client() as client:
            self._login(client, self.owner.id)
            response = client.post(f"/archive/delete/{file_id}")

        self.assertEqual(response.status_code, 302)
        self.assertFalse(db.session.get(ArchivedFile, file_id).is_deleted)


if __name__ == "__main__":
    unittest.main()
