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
    InvFixedAsset,
    InvFixedAssetCycle,
    InvFixedAssetEntry,
    InvItem,
    InvItemAttribute,
    InvItemCategory,
    InvWarehouse,
    User,
)
from portal import portal_bp


class FixedAssetRouteTests(unittest.TestCase):
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
            SECRET_KEY="fixed-asset-routes-test",
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
                    "{% block inv_content %}{% endblock %}"
                    "{% block scripts %}{% endblock %}"
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
            email="fixed-assets-admin@example.test",
            name="مسؤول الأصول",
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

    def _create_asset(self, tag="FA-ROUTE-001"):
        asset = InvFixedAsset(
            asset_tag=tag,
            name="كرسي مكتب",
            warehouse_id=self.warehouse.id,
            asset_condition="GOOD",
            lifecycle_status="ACTIVE",
            is_active=True,
            created_by_id=self.admin.id,
        )
        db.session.add(asset)
        db.session.commit()
        return asset

    def test_asset_crud_qr_labels_and_export(self):
        response = self.client.post(
            "/portal/inventory/fixed-assets/assets/new",
            data={
                "asset_tag": "FA-WEB-001",
                "name": "طاولة اجتماعات",
                "warehouse_id": str(self.warehouse.id),
                "asset_condition": "GOOD",
                "lifecycle_status": "ACTIVE",
                "is_active": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        asset = InvFixedAsset.query.filter_by(asset_tag="FA-WEB-001").one()

        self.assertEqual(self.client.get("/portal/inventory/fixed-assets").status_code, 200)
        self.assertEqual(self.client.get("/portal/inventory/fixed-assets/assets").status_code, 200)
        self.assertEqual(
            self.client.get(f"/portal/inventory/fixed-assets/assets/{asset.id}").status_code,
            200,
        )
        qr_response = self.client.get(
            f"/portal/inventory/fixed-assets/assets/{asset.id}/qr.png"
        )
        self.assertEqual(qr_response.status_code, 200)
        self.assertEqual(qr_response.mimetype, "image/png")
        self.assertEqual(
            self.client.get(f"/portal/inventory/fixed-assets/labels?ids={asset.id}").status_code,
            200,
        )
        export_response = self.client.get(
            "/portal/inventory/fixed-assets/assets/export.xlsx?active=all"
        )
        self.assertEqual(export_response.status_code, 200)
        self.assertIn("spreadsheetml", export_response.mimetype)

    def test_cycle_lookup_scan_and_close(self):
        asset = self._create_asset()
        response = self.client.post(
            "/portal/inventory/fixed-assets/cycles/new",
            data={
                "code": "FA-INV-WEB",
                "name": "جرد الويب",
                "inventory_date": "2026-09-10",
                "committee_name": "لجنة الاختبار",
                "scope_warehouse_id": str(self.warehouse.id),
                "chair_user_id": str(self.admin.id),
            },
        )
        self.assertEqual(response.status_code, 302)
        cycle = InvFixedAssetCycle.query.filter_by(code="FA-INV-WEB").one()
        entry = InvFixedAssetEntry.query.filter_by(cycle_id=cycle.id, asset_id=asset.id).one()
        self.assertEqual(entry.result_status, "PENDING")

        edit_response = self.client.post(
            f"/portal/inventory/fixed-assets/cycles/{cycle.id}/edit",
            data={
                "code": cycle.code,
                "name": "جرد الويب المعدل",
                "inventory_date": cycle.inventory_date,
                "committee_name": "لجنة الاختبار المعدلة",
                "chair_user_id": str(self.admin.id),
            },
        )
        self.assertEqual(edit_response.status_code, 302)
        self.assertEqual(cycle.committee_name, "لجنة الاختبار المعدلة")
        self.assertEqual(len(cycle.members), 1)

        lookup_response = self.client.get(
            f"/portal/inventory/fixed-assets/cycles/{cycle.id}/scan/lookup?code={asset.asset_tag}"
        )
        self.assertEqual(lookup_response.status_code, 200)
        self.assertEqual(lookup_response.get_json()["asset"]["id"], asset.id)

        scan_response = self.client.post(
            f"/portal/inventory/fixed-assets/cycles/{cycle.id}/scan",
            data={
                "asset_code": asset.qr_token,
                "observed_warehouse_id": str(self.warehouse.id),
                "observed_condition": "GOOD",
                "observed_lifecycle_status": "ACTIVE",
                "scan_source": "CAMERA",
            },
        )
        self.assertEqual(scan_response.status_code, 302)
        self.assertEqual(entry.result_status, "MATCHED")

        close_response = self.client.post(
            f"/portal/inventory/fixed-assets/cycles/{cycle.id}/close",
            data={"apply_observed_values": "1"},
        )
        self.assertEqual(close_response.status_code, 302)
        self.assertEqual(cycle.status, "CLOSED")
        self.assertEqual(
            self.client.get(f"/portal/inventory/fixed-assets/cycles/{cycle.id}").status_code,
            200,
        )
        self.assertEqual(
            self.client.get(f"/portal/inventory/fixed-assets/cycles/{cycle.id}/export.xlsx").status_code,
            200,
        )

    def test_xlsx_import_creates_asset(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["رقم الأصل", "اسم الأصل", "المستودع", "حالة الأصل", "وضع الأصل"])
        sheet.append(["FA-IMPORT-001", "حاسوب محمول", "MAIN", "جيد", "قيد الاستخدام"])
        payload = io.BytesIO()
        workbook.save(payload)
        payload.seek(0)

        response = self.client.post(
            "/portal/inventory/fixed-assets/assets/import",
            data={"file": (payload, "assets.xlsx")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        asset = InvFixedAsset.query.filter_by(asset_tag="FA-IMPORT-001").one()
        self.assertEqual(asset.name, "حاسوب محمول")
        self.assertEqual(asset.warehouse_id, self.warehouse.id)

    def test_ministry_catalog_import_creates_items_and_is_idempotent(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Items"
        sheet.append(["Code", "Name", "Category", "Subcategory", "Consumable", "Unit"])
        sheet.append(["010010001", "Vertical blinds", "Furniture", "Blinds", "YES", "m2"])
        sheet.append(["010010002", "Roller blinds", "Furniture", "Blinds", "NO", "PCS"])
        payload = io.BytesIO()
        workbook.save(payload)
        payload_bytes = payload.getvalue()

        response = self.client.post(
            "/portal/inventory/admin/items/import-catalog",
            data={"file": (io.BytesIO(payload_bytes), "jard.xlsx")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(InvItemCategory.query.count(), 1)
        self.assertEqual(InvItem.query.count(), 2)
        first_item = InvItem.query.filter_by(code="010010001").one()
        self.assertEqual(first_item.category.name, "Furniture")
        self.assertEqual(first_item.unit, "m2")
        self.assertEqual(
            {(row.name, row.value) for row in InvItemAttribute.query.filter_by(item_id=first_item.id)},
            {("التصنيف الفرعي", "Blinds"), ("مستهلك", "YES")},
        )

        response = self.client.post(
            "/portal/inventory/admin/items/import-catalog",
            data={"file": (io.BytesIO(payload_bytes), "jard.xlsx")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(InvItem.query.count(), 2)


if __name__ == "__main__":
    unittest.main()
