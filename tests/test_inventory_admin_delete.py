import tempfile
import unittest
from pathlib import Path

from flask import Flask
from flask_login import LoginManager
from jinja2 import ChoiceLoader, DictLoader

from extensions import db
from models import (
    InvInboundVoucher,
    InvInboundVoucherLine,
    InvAdjustmentVoucher,
    InvAdjustmentVoucherLine,
    InvIssueVoucher,
    InvIssueVoucherLine,
    InvItem,
    InvReturnVoucher,
    InvReturnVoucherLine,
    InvRoom,
    InvStocktakeVoucher,
    InvSupplier,
    InvWarehouse,
    User,
)
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

    def test_item_card_report_shows_signed_net_quantity(self):
        item = InvItem(name="قهوة عربية", code="COFFEE-001")
        db.session.add(item)
        db.session.flush()

        inbound = InvInboundVoucher(
            voucher_no="IN-TEST-001",
            voucher_date="2026-11-02",
            to_warehouse_id=self.warehouse.id,
            created_by_id=self.admin.id,
        )
        issue = InvIssueVoucher(
            issue_kind="ROOM",
            voucher_no="EMP-TEST-001",
            voucher_date="2026-11-02",
            from_warehouse_id=self.warehouse.id,
            to_room_name="غرفة الاختبار",
            created_by_id=self.admin.id,
        )
        returned = InvReturnVoucher(
            voucher_no="RET-TEST-001",
            voucher_date="2026-11-02",
            to_warehouse_id=self.warehouse.id,
            from_room_name="غرفة الاختبار",
            created_by_id=self.admin.id,
        )
        db.session.add_all((inbound, issue, returned))
        db.session.flush()
        db.session.add_all((
            InvInboundVoucherLine(voucher_id=inbound.id, item_id=item.id, qty=10),
            InvIssueVoucherLine(voucher_id=issue.id, item_id=item.id, qty=2),
            InvReturnVoucherLine(voucher_id=returned.id, item_id=item.id, qty=3),
        ))
        db.session.commit()

        response = self.client.get(
            f"/portal/inventory/reports/item-card?item_id={item.id}"
            f"&warehouse_id={self.warehouse.id}"
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("10.00", body)
        self.assertIn("-2.00", body)
        self.assertIn("3.00", body)
        self.assertIn("11.00", body)

    def test_adjustment_voucher_applies_signed_delta_and_is_visible_in_reports(self):
        item = InvItem(name="Correction item", code="CORR-001")
        db.session.add(item)
        db.session.commit()

        new_response = self.client.get("/portal/inventory/vouchers/adjustment/new")
        list_response = self.client.get("/portal/inventory/vouchers/adjustment/list")
        self.assertEqual(new_response.status_code, 200)
        self.assertEqual(list_response.status_code, 200)

        response = self.client.post(
            "/portal/inventory/vouchers/adjustment/new",
            data={
                "voucher_date": "2026-11-02",
                "warehouse_id": str(self.warehouse.id),
                "reason": "Wrong inbound quantity",
                "source_reference": "IN-TEST-001",
                "item_id[]": [str(item.id)],
                "delta_qty[]": ["-3"],
            },
        )

        self.assertEqual(response.status_code, 302)
        voucher = InvAdjustmentVoucher.query.one()
        line = InvAdjustmentVoucherLine.query.filter_by(voucher_id=voucher.id).one()
        self.assertEqual(voucher.voucher_no, "ADJ-2026-000001")
        self.assertEqual(line.delta_qty, -3.0)

        card_response = self.client.get(
            f"/portal/inventory/reports/item-card?item_id={item.id}"
            f"&warehouse_id={self.warehouse.id}"
        )
        self.assertEqual(card_response.status_code, 200)
        card_body = card_response.get_data(as_text=True)
        self.assertIn("-3.00", card_body)

        view_response = self.client.get(
            f"/portal/inventory/vouchers/adjustment/{voucher.id}"
        )
        self.assertEqual(view_response.status_code, 200)
        view_body = view_response.get_data(as_text=True)
        self.assertIn("ADJ-2026-000001", view_body)
        self.assertIn("Wrong inbound quantity", view_body)

        yearly_response = self.client.get(
            "/portal/inventory/reports/yearly-qty?year=2026"
        )
        self.assertEqual(yearly_response.status_code, 200)
        self.assertIn("-3.00", yearly_response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
