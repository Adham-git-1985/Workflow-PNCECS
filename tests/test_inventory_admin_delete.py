import tempfile
import unittest
from pathlib import Path

from flask import Flask
from flask_login import LoginManager
from jinja2 import ChoiceLoader, DictLoader

from extensions import db
from models import InvRoom, InvStocktakeVoucher, InvSupplier, InvWarehouse, User
from portal import portal_bp


class InventoryAdminDeleteTests(unittest.TestCase):
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
            SECRET_KEY="inventory-admin-delete-test",
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
            email="inventory-admin-delete@example.test",
            name="مسؤول المستودع",
            password_hash="not-used",
            role="SUPER_ADMIN",
        )
        self.warehouse = InvWarehouse(name="مخزن الاختبار", code="TEST-WH")
        self.room = InvRoom(name="غرفة الاختبار", code="TEST-RM")
        self.supplier = InvSupplier(name="مورد الاختبار")
        db.session.add_all([self.admin, self.warehouse, self.room, self.supplier])
        db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["_user_id"] = str(self.admin.id)
            session["_fresh"] = True

    def test_admin_pages_expose_delete_actions(self):
        for path in (
            "/portal/inventory/admin/warehouses",
            "/portal/inventory/admin/rooms",
            "/portal/inventory/admin/suppliers",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'name="action" value="delete"', response.data)

    def test_used_warehouse_is_disabled_instead_of_deleted(self):
        voucher = InvStocktakeVoucher(
            voucher_no="STK-TEST-001",
            voucher_date="2026-09-29",
            warehouse_id=self.warehouse.id,
        )
        db.session.add(voucher)
        db.session.commit()

        response = self.client.post(
            "/portal/inventory/admin/warehouses",
            data={"action": "delete", "id": str(self.warehouse.id)},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        warehouse = db.session.get(InvWarehouse, self.warehouse.id)
        self.assertIsNotNone(warehouse)
        self.assertFalse(warehouse.is_active)
        self.assertIn("تم تعطيله", response.get_data(as_text=True))

    def test_unused_room_and_supplier_are_deleted(self):
        room_id = self.room.id
        supplier_id = self.supplier.id

        room_response = self.client.post(
            "/portal/inventory/admin/rooms",
            data={"action": "delete", "id": str(room_id)},
        )
        supplier_response = self.client.post(
            "/portal/inventory/admin/suppliers",
            data={"action": "delete", "id": str(supplier_id)},
        )

        self.assertEqual(room_response.status_code, 302)
        self.assertEqual(supplier_response.status_code, 302)
        self.assertIsNone(db.session.get(InvRoom, room_id))
        self.assertIsNone(db.session.get(InvSupplier, supplier_id))


if __name__ == "__main__":
    unittest.main()
