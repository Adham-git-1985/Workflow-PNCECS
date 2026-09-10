import importlib
import tempfile
import unittest
from datetime import datetime

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from flask import Flask

from extensions import db
from models import (
    InvFixedAsset,
    InvFixedAssetCycle,
    InvFixedAssetEntry,
    InvFixedAssetScanLog,
    InvRoom,
    InvWarehouse,
    User,
)
from services.fixed_asset_inventory import (
    close_inventory_cycle,
    find_asset_by_identifier,
    normalize_asset_identifier,
    populate_cycle_entries,
    record_asset_scan,
    reopen_inventory_cycle,
)


class FixedAssetInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.app = Flask(__name__, instance_path=cls.temp_dir.name)
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="fixed-assets-test",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
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
        self.user = User(
            email="inventory@example.test",
            name="عضو لجنة الجرد",
            password_hash="not-used",
            role="EMPLOYEE",
        )
        self.warehouse_a = InvWarehouse(name="المستودع الأول", code="WH-A")
        self.warehouse_b = InvWarehouse(name="المستودع الثاني", code="WH-B")
        self.room_a = InvRoom(name="الغرفة الأولى", code="R-A")
        self.room_b = InvRoom(name="الغرفة الثانية", code="R-B")
        db.session.add_all([
            self.user,
            self.warehouse_a,
            self.warehouse_b,
            self.room_a,
            self.room_b,
        ])
        db.session.commit()

    def _asset(self, tag, warehouse=None, room=None, condition="GOOD"):
        asset = InvFixedAsset(
            asset_tag=tag,
            name=f"أصل {tag}",
            warehouse_id=(warehouse or self.warehouse_a).id,
            room_id=(room or self.room_a).id,
            asset_condition=condition,
            lifecycle_status="ACTIVE",
            is_active=True,
            created_by_id=self.user.id,
        )
        db.session.add(asset)
        db.session.commit()
        return asset

    def _cycle(self, code, previous=None):
        cycle = InvFixedAssetCycle(
            code=code,
            name=f"دورة {code}",
            inventory_date="2026-09-10",
            previous_cycle_id=previous.id if previous else None,
            status="ACTIVE",
            created_by_id=self.user.id,
        )
        db.session.add(cycle)
        db.session.commit()
        return cycle

    def test_cycle_uses_previous_committee_observation_as_baseline(self):
        asset = self._asset("FA-000001")
        previous = self._cycle("PREVIOUS")
        previous.status = "CLOSED"
        previous_entry = InvFixedAssetEntry(
            cycle_id=previous.id,
            asset_id=asset.id,
            was_expected=True,
            expected_warehouse_id=self.warehouse_a.id,
            expected_room_id=self.room_a.id,
            expected_condition="GOOD",
            expected_lifecycle_status="ACTIVE",
            observed_warehouse_id=self.warehouse_b.id,
            observed_room_id=self.room_b.id,
            observed_condition="FAIR",
            observed_lifecycle_status="ACTIVE",
            result_status="MOVED_AND_CONDITION_CHANGED",
            scanned_at=datetime.utcnow(),
            scan_count=1,
        )
        db.session.add(previous_entry)
        db.session.commit()

        current = self._cycle("CURRENT", previous=previous)
        self.assertEqual(populate_cycle_entries(current), 1)
        db.session.commit()

        entry = InvFixedAssetEntry.query.filter_by(cycle_id=current.id, asset_id=asset.id).one()
        self.assertEqual(entry.expected_warehouse_id, self.warehouse_b.id)
        self.assertEqual(entry.expected_room_id, self.room_b.id)
        self.assertEqual(entry.expected_condition, "FAIR")
        self.assertEqual(entry.previous_result_status, "MOVED_AND_CONDITION_CHANGED")

    def test_scan_detects_match_then_condition_change_and_keeps_log(self):
        asset = self._asset("FA-000002")
        cycle = self._cycle("SCAN")
        populate_cycle_entries(cycle)
        db.session.commit()

        entry = record_asset_scan(
            cycle,
            asset,
            scanned_by_id=self.user.id,
            observed_warehouse_id=self.warehouse_a.id,
            observed_room_id=self.room_a.id,
            observed_custodian_user_id=None,
            observed_condition="GOOD",
            observed_lifecycle_status="ACTIVE",
            source="CAMERA",
        )
        db.session.commit()
        self.assertEqual(entry.result_status, "MATCHED")

        entry = record_asset_scan(
            cycle,
            asset,
            scanned_by_id=self.user.id,
            observed_warehouse_id=self.warehouse_a.id,
            observed_room_id=self.room_a.id,
            observed_custodian_user_id=None,
            observed_condition="DAMAGED",
            observed_lifecycle_status="ACTIVE",
            note="يحتاج صيانة",
            source="MANUAL",
        )
        db.session.commit()
        self.assertEqual(entry.result_status, "CONDITION_CHANGED")
        self.assertEqual(entry.scan_count, 2)
        self.assertEqual(InvFixedAssetScanLog.query.filter_by(entry_id=entry.id).count(), 2)

    def test_scoped_cycle_includes_assets_moved_into_or_out_of_scope(self):
        moved_in = self._asset("FA-SCOPE-IN", warehouse=self.warehouse_a)
        moved_out = self._asset("FA-SCOPE-OUT", warehouse=self.warehouse_b)
        previous = self._cycle("SCOPE-PREVIOUS")
        previous.status = "CLOSED"
        db.session.add_all([
            InvFixedAssetEntry(
                cycle_id=previous.id,
                asset_id=moved_in.id,
                was_expected=True,
                expected_warehouse_id=self.warehouse_b.id,
                expected_condition="GOOD",
                expected_lifecycle_status="ACTIVE",
                observed_warehouse_id=self.warehouse_b.id,
                observed_condition="GOOD",
                observed_lifecycle_status="ACTIVE",
                result_status="MATCHED",
                scanned_at=datetime.utcnow(),
                scan_count=1,
            ),
            InvFixedAssetEntry(
                cycle_id=previous.id,
                asset_id=moved_out.id,
                was_expected=True,
                expected_warehouse_id=self.warehouse_a.id,
                expected_condition="GOOD",
                expected_lifecycle_status="ACTIVE",
                observed_warehouse_id=self.warehouse_a.id,
                observed_condition="GOOD",
                observed_lifecycle_status="ACTIVE",
                result_status="MATCHED",
                scanned_at=datetime.utcnow(),
                scan_count=1,
            ),
        ])
        db.session.commit()

        current = self._cycle("SCOPE-CURRENT", previous=previous)
        current.scope_warehouse_id = self.warehouse_a.id
        self.assertEqual(populate_cycle_entries(current), 2)
        db.session.commit()
        self.assertEqual(
            {entry.asset_id for entry in current.entries},
            {moved_in.id, moved_out.id},
        )

    def test_missing_asset_found_by_next_committee_is_recovered(self):
        asset = self._asset("FA-000003")
        previous = self._cycle("MISSING-PREVIOUS")
        previous.status = "CLOSED"
        db.session.add(InvFixedAssetEntry(
            cycle_id=previous.id,
            asset_id=asset.id,
            was_expected=True,
            expected_warehouse_id=self.warehouse_a.id,
            expected_room_id=self.room_a.id,
            expected_condition="GOOD",
            expected_lifecycle_status="ACTIVE",
            result_status="MISSING",
            scan_count=0,
        ))
        db.session.commit()

        current = self._cycle("RECOVERY", previous=previous)
        populate_cycle_entries(current)
        db.session.commit()
        entry = record_asset_scan(
            current,
            asset,
            scanned_by_id=self.user.id,
            observed_warehouse_id=self.warehouse_a.id,
            observed_room_id=self.room_a.id,
            observed_custodian_user_id=None,
            observed_condition="GOOD",
            observed_lifecycle_status="ACTIVE",
        )
        db.session.commit()
        self.assertEqual(entry.result_status, "RECOVERED")

    def test_close_marks_missing_and_can_reconcile_then_reopen(self):
        moved_asset = self._asset("FA-000004")
        missing_asset = self._asset("FA-000005")
        cycle = self._cycle("CLOSE")
        populate_cycle_entries(cycle)
        db.session.commit()
        record_asset_scan(
            cycle,
            moved_asset,
            scanned_by_id=self.user.id,
            observed_warehouse_id=self.warehouse_b.id,
            observed_room_id=self.room_b.id,
            observed_custodian_user_id=self.user.id,
            observed_condition="FAIR",
            observed_lifecycle_status="ACTIVE",
        )
        db.session.commit()

        result = close_inventory_cycle(
            cycle,
            closed_by_id=self.user.id,
            apply_observed_values=True,
        )
        db.session.commit()
        self.assertEqual(result, {"missing": 1, "reconciled": 1})
        self.assertEqual(moved_asset.warehouse_id, self.warehouse_b.id)
        self.assertEqual(moved_asset.room_id, self.room_b.id)
        self.assertEqual(moved_asset.asset_condition, "FAIR")
        missing_entry = InvFixedAssetEntry.query.filter_by(
            cycle_id=cycle.id,
            asset_id=missing_asset.id,
        ).one()
        self.assertEqual(missing_entry.result_status, "MISSING")

        self.assertEqual(reopen_inventory_cycle(cycle), 1)
        db.session.commit()
        self.assertEqual(missing_entry.result_status, "PENDING")
        self.assertEqual(cycle.status, "ACTIVE")

    def test_qr_url_and_tag_resolve_to_asset(self):
        asset = self._asset("FA-000006")
        qr_url = f"https://inventory.example/portal/inventory/fixed-assets/qr/{asset.qr_token}"
        self.assertEqual(normalize_asset_identifier(qr_url), asset.qr_token)
        self.assertEqual(find_asset_by_identifier(qr_url).id, asset.id)
        self.assertEqual(find_asset_by_identifier("fa:FA-000006").id, asset.id)


class FixedAssetMigrationTests(unittest.TestCase):
    def test_migration_is_safe_after_runtime_schema_creation(self):
        migration = importlib.import_module(
            "migrations.versions.j1k2l3m4n5o6_add_fixed_asset_qr_inventory"
        )
        engine = sa.create_engine("sqlite:///:memory:")
        metadata = sa.MetaData()
        sa.Table("users", metadata, sa.Column("id", sa.Integer, primary_key=True))
        sa.Table("inv_item", metadata, sa.Column("id", sa.Integer, primary_key=True))
        sa.Table("inv_item_category", metadata, sa.Column("id", sa.Integer, primary_key=True))
        sa.Table("inv_warehouse", metadata, sa.Column("id", sa.Integer, primary_key=True))
        sa.Table("inv_room", metadata, sa.Column("id", sa.Integer, primary_key=True))
        metadata.create_all(engine)

        with engine.begin() as connection:
            operations = Operations(MigrationContext.configure(connection))
            original_operations = migration.op
            migration.op = operations
            try:
                migration.upgrade()
                migration.upgrade()
            finally:
                migration.op = original_operations

            tables = set(sa.inspect(connection).get_table_names())
            self.assertTrue({
                "inv_fixed_asset",
                "inv_fixed_asset_cycle",
                "inv_fixed_asset_cycle_member",
                "inv_fixed_asset_entry",
                "inv_fixed_asset_scan_log",
            }.issubset(tables))


if __name__ == "__main__":
    unittest.main()
