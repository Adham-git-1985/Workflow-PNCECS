import importlib
import unittest

import sqlalchemy as sa
from flask import g
from alembic.migration import MigrationContext
from alembic.operations import Operations

from extensions import db
from models import (InvAssetDocument, InvAssetDocumentReservation, InvFixedAssetCycle,
                    InvFixedAssetCycleMember, InvFixedAssetEntry, Notification, User)
from services.asset_custody import (cancel_document, create_movement, issue_document,
                                    review_document, send_cycle_reports)
from services.fixed_asset_inventory import close_inventory_cycle, populate_cycle_entries, record_asset_scan, reopen_inventory_cycle
import tests.test_fixed_asset_routes as fixtures


class AssetCustodyTests(unittest.TestCase):
    setUpClass = fixtures.FixedAssetRouteTests.__dict__["setUpClass"]
    tearDownClass = fixtures.FixedAssetRouteTests.__dict__["tearDownClass"]
    _create_asset = fixtures.FixedAssetRouteTests._create_asset

    def setUp(self):
        fixtures.FixedAssetRouteTests.setUp(self)
        self.employee = User(email="holder@example.test", name="موظف العهدة", role="EMPLOYEE", password_hash="unused")
        self.other = User(email="other@example.test", name="موظف آخر", role="EMPLOYEE", password_hash="unused")
        db.session.add_all([self.employee, self.other])
        db.session.commit()
        self.login(self.admin)

    def login(self, user):
        # The fixture holds one app context; real requests get a fresh g.
        for key in list(g):
            g.pop(key, None)
        with self.client.session_transaction() as session:
            session["_user_id"] = str(user.id)
            session["_fresh"] = True

    def cycle(self, asset, missing=False):
        cycle = InvFixedAssetCycle(code="TEST", name="جرد تجريبي", inventory_date="2026-09-11", status="ACTIVE")
        db.session.add(cycle)
        db.session.flush()
        populate_cycle_entries(cycle)
        if not missing:
            record_asset_scan(cycle, asset, scanned_by_id=self.admin.id,
                observed_warehouse_id=self.warehouse.id, observed_room_id=None,
                observed_custodian_user_id=self.employee.id, observed_condition="GOOD", observed_lifecycle_status="ACTIVE")
        close_inventory_cycle(cycle, closed_by_id=self.admin.id, apply_observed_values=True)
        db.session.commit()
        return cycle

    def approve(self, doc):
        review_document(doc, doc.employee_id, {line.id: ("ACCEPT", "موافق") for line in doc.lines})
        db.session.commit()

    def test_full_inventory_review_issue_and_notifications(self):
        asset = self._create_asset()
        cycle = self.cycle(asset)
        self.assertIsNone(asset.custodian_user_id, "Scanning/closing must not assign custody before review")
        response = self.client.post(f"/portal/inventory/fixed-assets/cycles/{cycle.id}/send-reports")
        self.assertEqual(response.status_code, 302)
        doc = InvAssetDocument.query.one()
        self.assertEqual(doc.employee_id, self.employee.id)
        self.assertTrue(Notification.query.filter_by(user_id=self.employee.id).first())
        self.login(self.employee)
        endpoint = f"/portal/inventory/fixed-assets/documents/{doc.id}"
        page = self.client.get(endpoint)
        self.assertEqual(page.status_code, 200)
        self.assertIn("ملاحظات الموظف", page.get_data(as_text=True))
        line = doc.lines[0]
        response = self.client.post(endpoint, data={"action":"review", f"decision_{line.id}":"ACCEPT", f"note_{line.id}":"تمت المراجعة"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(doc.status, "APPROVED")
        self.assertIsNone(asset.custodian_user_id)
        self.assertEqual(self.client.post(endpoint, data={"action":"issue"}).status_code, 403)
        self.login(self.admin)
        self.assertEqual(self.client.post(endpoint, data={"action":"issue"}).status_code, 302)
        self.assertEqual(doc.status, "ISSUED")
        self.assertEqual(asset.custodian_user_id, self.employee.id)
        self.assertEqual(InvAssetDocumentReservation.query.count(), 0)
        self.assertIn("سند عهدة", self.client.get(endpoint).get_data(as_text=True))
        with self.assertRaises(ValueError):
            issue_document(doc, self.admin.id)
        db.session.rollback()

    def test_unrelated_employee_cannot_read_or_review(self):
        doc = create_movement(self._create_asset(), "ASSIGN", self.employee.id, self.admin.id, "تجهيز المكتب")
        db.session.commit()
        self.login(self.other)
        path = f"/portal/inventory/fixed-assets/documents/{doc.id}"
        self.assertEqual(self.client.get(path).status_code, 403)
        self.assertEqual(self.client.post(path, data={"action":"review"}).status_code, 403)
        self.assertNotIn("تجهيز المكتب", self.client.get("/portal/inventory/fixed-assets/documents").get_data(as_text=True))

    def test_objection_requires_note_and_all_lines_reviewed(self):
        asset = self._create_asset()
        doc = create_movement(asset, "ASSIGN", self.employee.id, self.admin.id, "تكليف")
        db.session.commit()
        for responses in ({}, {doc.lines[0].id:("OBJECT", "")}):
            with self.assertRaises(ValueError):
                review_document(doc, self.employee.id, responses)
            db.session.rollback()
        review_document(doc, self.employee.id, {doc.lines[0].id:("OBJECT", "الرقم التسلسلي مختلف")})
        db.session.commit()
        self.assertEqual(doc.status, "REJECTED")
        self.assertEqual(InvAssetDocumentReservation.query.count(), 0)
        with self.assertRaises(ValueError):
            issue_document(doc, self.admin.id)
        db.session.rollback()
        self.assertIsNone(asset.custodian_user_id)

    def test_transfer_return_and_disposal_preserve_history(self):
        asset = self._create_asset()
        asset.custodian_user_id = self.employee.id
        db.session.commit()
        doc = create_movement(asset, "TRANSFER", self.other.id, self.admin.id, "نقل إلى مكتب آخر")
        db.session.commit()
        self.assertEqual(asset.custodian_user_id, self.employee.id)
        self.approve(doc)
        issue_document(doc, self.admin.id)
        db.session.commit()
        self.assertEqual(asset.custodian_user_id, self.other.id)
        self.assertEqual(doc.lines[0].snapshot["state"]["custodian_user_id"], self.employee.id)
        self.login(self.employee)
        self.assertEqual(self.client.get(f"/portal/inventory/fixed-assets/documents/{doc.id}").status_code, 200)
        returned = create_movement(asset, "RETURN", None, self.admin.id, "إرجاع للمستودع")
        db.session.commit()
        self.assertEqual(returned.status, "COMPLETED")
        self.assertIsNone(returned.reviewed_at)
        self.assertIsNone(asset.custodian_user_id)
        self.assertEqual(asset.lifecycle_status, "IN_STORE")
        disposed = create_movement(asset, "DISPOSE", None, self.admin.id, "قرار لجنة الإتلاف رقم 5")
        db.session.commit()
        self.assertEqual(disposed.status, "COMPLETED")
        self.assertFalse(asset.is_active)
        self.assertEqual(asset.lifecycle_status, "DISPOSED")
        with self.assertRaises(ValueError):
            create_movement(asset, "ASSIGN", self.employee.id, self.admin.id, "غير مسموح")

    def test_stale_snapshot_rolls_back_issue(self):
        asset = self._create_asset()
        doc = create_movement(asset, "ASSIGN", self.employee.id, self.admin.id, "تكليف")
        db.session.commit()
        self.approve(doc)
        asset.asset_condition = "DAMAGED"
        db.session.commit()
        with self.assertRaises(ValueError):
            issue_document(doc, self.admin.id)
        db.session.rollback()
        self.assertEqual(doc.status, "APPROVED")
        self.assertIsNone(asset.custodian_user_id)

    def test_open_documents_reserve_assets_and_cancel_releases(self):
        asset = self._create_asset()
        doc = create_movement(asset, "ASSIGN", self.employee.id, self.admin.id, "تكليف")
        db.session.commit()
        with self.assertRaises(ValueError):
            create_movement(asset, "ASSIGN", self.other.id, self.admin.id, "مكرر")
        db.session.rollback()
        cancel_document(doc)
        db.session.commit()
        self.assertEqual(InvAssetDocumentReservation.query.count(), 0)
        create_movement(asset, "ASSIGN", self.other.id, self.admin.id, "تصحيح")
        db.session.commit()
        self.assertEqual(InvAssetDocument.query.count(), 2)

    def test_missing_asset_is_reviewed_but_not_assigned(self):
        asset = self._create_asset()
        asset.custodian_user_id = self.employee.id
        db.session.commit()
        cycle = self.cycle(asset, missing=True)
        doc = send_cycle_reports(cycle, self.admin.id)[0]
        db.session.commit()
        self.assertFalse(doc.lines[0].snapshot["eligible"])
        self.approve(doc)
        issue_document(doc, self.admin.id)
        db.session.commit()
        self.assertEqual(asset.custodian_user_id, self.employee.id)
        self.assertEqual(asset.lifecycle_status, "ACTIVE")
        self.assertEqual(doc.lines[0].snapshot["result"], "MISSING")

    def test_reports_freeze_cycle_and_duplicate_send_is_rejected(self):
        cycle = self.cycle(self._create_asset())
        send_cycle_reports(cycle, self.admin.id)
        db.session.commit()
        with self.assertRaises(ValueError):
            send_cycle_reports(cycle, self.admin.id)
        with self.assertRaises(ValueError):
            reopen_inventory_cycle(cycle)
        cancel_document(InvAssetDocument.query.one())
        db.session.commit()
        reopen_inventory_cycle(cycle)
        db.session.commit()
        self.assertEqual(cycle.status, "ACTIVE")

    def test_committee_member_scans_without_store_management(self):
        asset = self._create_asset()
        cycle = InvFixedAssetCycle(code="MOBILE", name="جرد الهاتف", inventory_date="2026-09-11", status="ACTIVE")
        db.session.add(cycle)
        db.session.flush()
        db.session.add(InvFixedAssetCycleMember(cycle_id=cycle.id, user_id=self.employee.id))
        db.session.commit()
        self.login(self.employee)
        path = f"/portal/inventory/fixed-assets/cycles/{cycle.id}/scan"
        self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.get(path + f"/lookup?code={asset.qr_token}").status_code, 200)
        self.assertEqual(self.client.post(path, data={"asset_code":asset.qr_token, "observed_condition":"GOOD", "scan_source":"CAMERA"}).status_code, 302)
        self.assertEqual(InvFixedAssetEntry.query.one().scan_count, 1)
        self.assertEqual(self.client.post(f"/portal/inventory/fixed-assets/cycles/{cycle.id}/close").status_code, 403)
        self.login(self.other)
        self.assertEqual(self.client.get(path).status_code, 403)
        self.assertEqual(self.client.get(path + f"/lookup?code={asset.qr_token}").status_code, 403)

    def test_mobile_url_settings_and_csrf_protection(self):
        path = "/portal/inventory/fixed-assets/mobile-settings"
        self.assertEqual(self.client.post(path, data={"base_url":"https://inventory.example.test"}).status_code, 200)
        self.assertIn("https://inventory.example.test/portal/inventory/fixed-assets/mobile", self.client.get(path).get_data(as_text=True))
        self.assertEqual(self.client.get("/portal/inventory/fixed-assets/mobile-entry.png").mimetype, "image/png")
        self.app.config["WTF_CSRF_ENABLED"] = True
        try:
            self.assertEqual(self.client.post(path, data={"base_url":"https://bad.test"}).status_code, 400)
        finally:
            self.app.config["WTF_CSRF_ENABLED"] = False

    def test_migration_is_repeatable_after_runtime_create_all(self):
        migration = importlib.import_module("migrations.versions.k2l3m4n5o6p7_asset_custody_documents")
        with db.engine.begin() as connection:
            old = migration.op
            migration.op = Operations(MigrationContext.configure(connection))
            try:
                migration.upgrade()
                migration.downgrade()
                migration.upgrade()
                migration.upgrade()
            finally:
                migration.op = old
            self.assertIn("inv_asset_document_reservation", sa.inspect(connection).get_table_names())

    def test_local_jsqr_decodes_printed_label_pixels(self):
        import json
        import shutil
        import subprocess
        import qrcode
        from pathlib import Path
        if not shutil.which("node"):
            self.skipTest("Node.js is required to exercise the browser QR decoder")
        target = "https://inventory.example.test/portal/inventory/fixed-assets/qr/sample-token"
        image = qrcode.make(target).convert("RGBA")
        payload = json.dumps({"width": image.width, "height": image.height, "pixels": list(image.tobytes())})
        decoder = Path(__file__).resolve().parents[1] / "static/vendor/jsqr/jsQR.js"
        script = "const fs=require('fs'); const qr=require(process.argv[1]); const p=JSON.parse(fs.readFileSync(0,'utf8')); const result=qr(Uint8ClampedArray.from(p.pixels),p.width,p.height); process.stdout.write(result ? result.data : '');"
        result = subprocess.run(["node", "-e", script, str(decoder)], input=payload, capture_output=True, text=True, timeout=30, check=True)
        self.assertEqual(result.stdout, target)
