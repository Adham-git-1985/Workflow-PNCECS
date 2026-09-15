import importlib
import unittest

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from utils.inventory_numbers import auto_inventory_code, auto_inventory_voucher_no


class InventoryIdentifierTests(unittest.TestCase):
    def test_master_codes_and_voucher_numbers_are_stable(self):
        self.assertEqual(auto_inventory_code("warehouse", 7), "WH-0007")
        self.assertEqual(auto_inventory_code("room", 12), "RM-0012")
        self.assertEqual(
            auto_inventory_voucher_no("inbound", "2026-09-15", 31),
            "IN-2026-000031",
        )
        self.assertEqual(
            auto_inventory_voucher_no("issue_transfer", "2026-09-15", 4),
            "TRF-2026-000004",
        )

    def test_identifier_migration_backfills_only_blank_values(self):
        migration = importlib.import_module(
            "migrations.versions.l4m5n6o7p8q9_add_inventory_auto_identifiers"
        )
        engine = sa.create_engine("sqlite:///:memory:")
        metadata = sa.MetaData()
        sa.Table(
            "inv_warehouse",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("code", sa.String(50)),
        )
        sa.Table(
            "inv_room",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("code", sa.String(80)),
        )
        for table_name in (
            "inv_issue_voucher",
            "inv_inbound_voucher",
            "inv_scrap_voucher",
            "inv_return_voucher",
            "inv_stocktake_voucher",
            "inv_custody_voucher",
        ):
            sa.Table(
                table_name,
                metadata,
                sa.Column("id", sa.Integer, primary_key=True),
                sa.Column("voucher_date", sa.String(10)),
                sa.Column("voucher_no", sa.String(80)),
                *([sa.Column("issue_kind", sa.String(20))] if table_name == "inv_issue_voucher" else []),
            )
        metadata.create_all(engine)

        with engine.begin() as connection:
            connection.execute(sa.text("INSERT INTO inv_warehouse (id, code) VALUES (1, NULL), (2, 'CUSTOM-WH')"))
            connection.execute(sa.text("INSERT INTO inv_room (id, code) VALUES (1, ''), (2, 'CUSTOM-RM')"))
            connection.execute(sa.text("INSERT INTO inv_inbound_voucher (id, voucher_date, voucher_no) VALUES (3, '2026-09-15', NULL), (4, '2026-09-15', 'EXTERNAL-4')"))

            operations = Operations(MigrationContext.configure(connection))
            original_operations = migration.op
            migration.op = operations
            try:
                migration.upgrade()
            finally:
                migration.op = original_operations

            self.assertEqual(
                connection.execute(sa.text("SELECT code FROM inv_warehouse WHERE id = 1")).scalar_one(),
                "WH-0001",
            )
            self.assertEqual(
                connection.execute(sa.text("SELECT code FROM inv_warehouse WHERE id = 2")).scalar_one(),
                "CUSTOM-WH",
            )
            self.assertEqual(
                connection.execute(sa.text("SELECT code FROM inv_room WHERE id = 1")).scalar_one(),
                "RM-0001",
            )
            self.assertEqual(
                connection.execute(sa.text("SELECT voucher_no FROM inv_inbound_voucher WHERE id = 3")).scalar_one(),
                "IN-2026-000003",
            )
            self.assertEqual(
                connection.execute(sa.text("SELECT voucher_no FROM inv_inbound_voucher WHERE id = 4")).scalar_one(),
                "EXTERNAL-4",
            )


if __name__ == "__main__":
    unittest.main()
