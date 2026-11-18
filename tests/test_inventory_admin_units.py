import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from flask import Flask
from flask_login import LoginManager
from jinja2 import ChoiceLoader, DictLoader

from extensions import db
from models import InvUnit, User
from portal import portal_bp


class InventoryAdminUnitTests(unittest.TestCase):
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
            WTF_CSRF_ENABLED=False,
            SECRET_KEY="inventory-admin-unit-test",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(cls.app)
        login_manager = LoginManager()
        login_manager.init_app(cls.app)

        @login_manager.user_loader
        def load_user(user_id):
            return db.session.get(User, int(user_id))

        cls.app.register_blueprint(portal_bp)
        cls.app.jinja_loader = ChoiceLoader([
            DictLoader({
                "portal/inventory/base.html": (
                    "{% with messages = get_flashed_messages() %}"
                    "{{ messages|join(' ') }}"
                    "{% endwith %}{% block inv_content %}{% endblock %}"
                ),
            }),
            cls.app.jinja_loader,
        ])
        cls.app.jinja_env.globals["csrf_token"] = lambda: "test-token"
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
        self.admin = User(
            email="inventory-admin-unit@example.test",
            name="Inventory administrator",
            password_hash="not-used",
            role="SUPER_ADMIN",
        )
        db.session.add(self.admin)
        db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["_user_id"] = str(self.admin.id)
            session["_fresh"] = True

    def test_create_respects_inactive_flag(self):
        response = self.client.post(
            "/portal/inventory/admin/units",
            data={"action": "create", "name": "Inactive unit", "is_active": "0"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        unit = InvUnit.query.one()
        self.assertEqual(unit.name, "Inactive unit")
        self.assertFalse(unit.is_active)

    def test_duplicate_create_returns_message_instead_of_500(self):
        db.session.add(
            InvUnit(
                name="Existing unit",
                is_active=True,
                created_at=datetime.utcnow(),
            )
        )
        db.session.commit()

        response = self.client.post(
            "/portal/inventory/admin/units",
            data={"action": "create", "name": "Existing unit", "is_active": "1"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("اسم الوحدة مستخدم مسبقًا.", response.get_data(as_text=True))
        self.assertEqual(InvUnit.query.count(), 1)

    def test_update_rejects_duplicate_name_without_changing_record(self):
        first = InvUnit(
            name="First unit",
            is_active=True,
            created_at=datetime.utcnow(),
        )
        second = InvUnit(
            name="Second unit",
            is_active=True,
            created_at=datetime.utcnow(),
        )
        db.session.add_all([first, second])
        db.session.commit()

        response = self.client.post(
            "/portal/inventory/admin/units",
            data={
                "action": "update",
                "id": str(second.id),
                "name": "First unit",
                "is_active": "1",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("اسم الوحدة مستخدم مسبقًا لوحدة أخرى.", response.get_data(as_text=True))
        self.assertEqual(db.session.get(InvUnit, second.id).name, "Second unit")


if __name__ == "__main__":
    unittest.main()
