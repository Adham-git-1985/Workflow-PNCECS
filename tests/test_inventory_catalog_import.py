import io
import tempfile
import unittest
from pathlib import Path

from flask import Flask
from flask_login import LoginManager
from jinja2 import ChoiceLoader, DictLoader
from openpyxl import Workbook

from extensions import db
from models import (
    InvItem,
    InvStocktakeVoucher,
    InvStocktakeVoucherLine,
    InvWarehouse,
    User,
)
from portal import portal_bp
from portal.routes import _inv_build_balances, _read_ministry_catalog


class InventoryCatalogImportTests(unittest.TestCase):
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
            SECRET_KEY="inventory-catalog-import-test",
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
                "portal/inventory/base.html": "{% with messages = get_flashed_messages() %}{{ messages|join(' ') }}{% endwith %}{% block inv_content %}{% endblock %}",
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
            email="inventory-admin@example.test",
            name="مسؤول المستودع",
            password_hash="not-used",
            role="SUPER_ADMIN",
        )
        self.warehouse = InvWarehouse(name="المستودع الرئيسي", code="MAIN")
        db.session.add_all([self.admin, self.warehouse])
        db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["_user_id"] = str(self.admin.id)
            session["_fresh"] = True

    @staticmethod
    def _workbook_bytes():
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "الأصناف"
        worksheet.append([
            "رمز الصنف",
            "اسم الصنف",
            "التصنيف الرئيسي",
            "التصنيف الفرعي",
            "مستهلك",
            "الوحدة",
            "الكمية",
            "الغرفة",
        ])
        worksheet.append([
            "TEST-001",
            "ورق اختبار",
            "قرطاسية",
            "A4",
            "YES",
            "PCS",
            7,
            "غرفة مدير المستودع",
        ])
        worksheet.append([
            "TEST-002",
            "قلم اختبار",
            "قرطاسية",
            "",
            "YES",
            "PCS",
            12,
            "غرفة مدير المستودع",
        ])
        output = io.BytesIO()
        workbook.save(output)
        return output.getvalue()

    def _post_import(self, include_warehouse=True):
        data = {
            "voucher_date": "2026-09-15",
            "file": (io.BytesIO(self._workbook_bytes()), "materials.xlsx"),
        }
        if include_warehouse:
            data["warehouse_id"] = str(self.warehouse.id)
        return self.client.post(
            "/portal/inventory/admin/items/import-catalog",
            data=data,
            content_type="multipart/form-data",
        )

    def test_import_reads_quantities_and_creates_opening_stocktake(self):
        response = self._post_import()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(InvItem.query.count(), 2)
        voucher = InvStocktakeVoucher.query.one()
        self.assertEqual(voucher.warehouse_id, self.warehouse.id)
        self.assertEqual(voucher.voucher_date, "2026-09-15")
        lines = InvStocktakeVoucherLine.query.filter_by(voucher_id=voucher.id).all()
        self.assertEqual(len(lines), 2)
        self.assertEqual(sum(line.qty for line in lines), 19.0)
        self.assertEqual(_inv_build_balances()[(self.warehouse.id, lines[0].item_id)], lines[0].qty)
        self.assertIn("2 صنفًا", response.get_data(as_text=True))
        self.assertIn("19", response.get_data(as_text=True))

    def test_stocktake_voucher_filter_searches_the_displayed_room_contents(self):
        self.assertEqual(self._post_import().status_code, 200)
        voucher = InvStocktakeVoucher.query.one()

        response = self.client.get(
            f"/portal/inventory/vouchers/stocktake/{voucher.id}?q=TEST-001"
        )

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("البحث في موجودات الغرفة", page)
        self.assertIn("ورق اختبار", page)
        self.assertNotIn("قلم اختبار", page)
        self.assertIn('value="TEST-001"', page)

    def test_reimport_does_not_add_the_same_opening_quantity_twice(self):
        self.assertEqual(self._post_import().status_code, 200)
        self.assertEqual(self._post_import().status_code, 200)

        self.assertEqual(InvStocktakeVoucher.query.count(), 2)
        balances = _inv_build_balances()
        self.assertEqual(sum(balances.values()), 19.0)

    def test_warehouse_is_required_before_import(self):
        response = self._post_import(include_warehouse=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(InvItem.query.count(), 0)
        self.assertEqual(InvStocktakeVoucher.query.count(), 0)

    def test_prepared_workbook_contains_a_numeric_quantity_column(self):
        workbook_path = Path(__file__).resolve().parents[1] / "مواد_مستهلكة_طلب_المواد_غرفة_مدير_المستودع.xlsx"
        with workbook_path.open("rb") as stream:
            rows, errors = _read_ministry_catalog(stream)

        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 20)
        self.assertEqual(sum(row["quantity"] for row in rows), 930.0)
        self.assertTrue(all(row["quantity"] is not None for row in rows))


if __name__ == "__main__":
    unittest.main()
