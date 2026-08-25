import tempfile
import unittest
from pathlib import Path

from flask import Flask
from flask_login import LoginManager
from jinja2 import ChoiceLoader, DictLoader

from extensions import db
from models import PortalCircular, PortalCircularAttachment, User, UserPermission
from portal import portal_bp
from workflow import workflow_bp


class CircularManagementRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        project_root = Path(__file__).resolve().parents[1]
        cls.app = Flask(
            __name__,
            instance_path=cls.temp_dir.name,
            template_folder=str(project_root / "templates"),
            static_folder=str(project_root / "static"),
        )
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="circular-management-test",
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
        cls.app.register_blueprint(workflow_bp)
        cls.app.jinja_loader = ChoiceLoader([
            DictLoader({"layout.html": "{% block content %}{% endblock %}"}),
            cls.app.jinja_loader,
        ])
        cls.context = cls.app.app_context()
        cls.context.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.context.pop()
        cls.temp_dir.cleanup()

    def setUp(self):
        db.drop_all()
        db.create_all()
        self.admin = User(
            email="circular-admin@example.test",
            name="Circular Admin",
            password_hash="not-used-in-test",
            role="SUPER_ADMIN",
        )
        self.employee = User(
            email="circular-user@example.test",
            name="Circular User",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        db.session.add_all([self.admin, self.employee])
        db.session.flush()
        db.session.add(UserPermission(
            user_id=self.employee.id,
            key="PORTAL_READ",
            is_allowed=True,
        ))
        self.circular = PortalCircular(
            title="تعميم اختبار الإدارة",
            body="محتوى التعميم",
            target_scope="ALL",
            is_active=True,
            created_by_user_id=self.admin.id,
        )
        db.session.add(self.circular)
        db.session.flush()
        self.attachment = PortalCircularAttachment(
            circular_id=self.circular.id,
            original_name="اختبار.pdf",
            stored_name="circular-management-test.pdf",
            mime_type="application/pdf",
            file_size=4,
            uploaded_by_user_id=self.admin.id,
        )
        db.session.add(self.attachment)
        db.session.commit()

        self.circular_id = self.circular.id
        self.attachment_id = self.attachment.id
        self.attachment_path = (
            Path(self.temp_dir.name)
            / "uploads"
            / "circulars"
            / str(self.circular_id)
            / self.attachment.stored_name
        )
        self.attachment_path.parent.mkdir(parents=True, exist_ok=True)
        self.attachment_path.write_bytes(b"%PDF")

    def _login(self, client, user_id: int):
        with client.session_transaction() as session:
            session.clear()
            session["_user_id"] = str(user_id)
            session["_fresh"] = True

    def test_toggle_then_delete_removes_row_and_file(self):
        with self.app.test_client() as client:
            self._login(client, self.admin.id)
            response = client.post(f"/portal/circulars/{self.circular_id}/toggle-active")
            self.assertEqual(response.status_code, 302)
            self.assertFalse(db.session.get(PortalCircular, self.circular_id).is_active)

            response = client.get("/workflow/circulars")
            self.assertEqual(response.status_code, 200)
            body = response.get_data(as_text=True)
            self.assertIn("تعميم اختبار الإدارة", body)
            self.assertIn("غير مفعّل", body)
            self.assertIn("تفعيل", body)
            self.assertIn("حذف", body)

            response = client.get(f"/workflow/circulars/{self.circular_id}")
            self.assertEqual(response.status_code, 200)
            detail_body = response.get_data(as_text=True)
            self.assertIn("مخفي عن المستخدمين", detail_body)
            self.assertIn("تفعيل وإظهار للمستخدمين", detail_body)

            self._login(client, self.admin.id)
            response = client.post(f"/portal/circulars/{self.circular_id}/delete")
            self.assertEqual(response.status_code, 302)

        self.assertIsNone(db.session.get(PortalCircular, self.circular_id))
        self.assertIsNone(db.session.get(PortalCircularAttachment, self.attachment_id))
        self.assertFalse(self.attachment_path.exists())
        self.assertFalse(self.attachment_path.parent.exists())


if __name__ == "__main__":
    unittest.main()
