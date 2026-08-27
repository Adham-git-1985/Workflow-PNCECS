import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "repair_org_structure_links.py"
)
spec = importlib.util.spec_from_file_location("repair_org_structure_links", MODULE_PATH)
repair = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = repair
spec.loader.exec_module(repair)


class RepairOrgStructureLinksTests(unittest.TestCase):
    def make_db(self, path: Path):
        conn = sqlite3.connect(path)
        try:
            conn.executescript("""
                CREATE TABLE org_node_types (
                    id INTEGER PRIMARY KEY,
                    code TEXT NOT NULL
                );
                CREATE TABLE org_nodes (
                    id INTEGER PRIMARY KEY,
                    type_id INTEGER,
                    parent_id INTEGER,
                    code TEXT,
                    name_ar TEXT,
                    legacy_type TEXT,
                    legacy_id INTEGER,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    updated_at DATETIME
                );
                CREATE TABLE sections (
                    id INTEGER PRIMARY KEY,
                    department_id INTEGER,
                    directorate_id INTEGER,
                    unit_id INTEGER
                );
                CREATE TABLE departments (
                    id INTEGER PRIMARY KEY,
                    name_ar TEXT
                );
                CREATE TABLE directorates (
                    id INTEGER PRIMARY KEY,
                    name_ar TEXT
                );
                CREATE TABLE units (
                    id INTEGER PRIMARY KEY,
                    name_ar TEXT
                );
                CREATE TABLE system_setting (
                    id INTEGER PRIMARY KEY,
                    key TEXT UNIQUE NOT NULL,
                    value TEXT
                );

                INSERT INTO org_node_types(id, code) VALUES (1, 'DEPARTMENT');
                INSERT INTO org_node_types(id, code) VALUES (2, 'SECTION');
                INSERT INTO departments(id, name_ar) VALUES (8, 'دائرة الإيسيسكو');
                INSERT INTO departments(id, name_ar) VALUES (9, 'دائرة الألكسو');
                INSERT INTO sections(id, department_id) VALUES (25, 9);

                INSERT INTO org_nodes(id, type_id, code, name_ar, is_active)
                VALUES (17, 1, 'DEP_ICESCO', 'دائرة الإيسيسكو', 1);
                INSERT INTO org_nodes(id, type_id, code, name_ar, is_active)
                VALUES (39, 1, 'DEP_ALECSO', 'دائرة الألكسو', 1);
                INSERT INTO org_nodes(id, type_id, parent_id, code, name_ar, is_active)
                VALUES (18, 2, 17, 'SEC_ICESCO_REL', 'قسم العلاقات الدولية', 1);

                INSERT INTO org_nodes(
                    id, type_id, code, name_ar, legacy_type, legacy_id, is_active
                ) VALUES (
                    90, 1, 'DEP-PNC-9', 'دائرة الألكسو', 'DEPARTMENT', 9, 1
                );
                INSERT INTO org_nodes(
                    id, type_id, parent_id, code, name_ar, legacy_type, legacy_id, is_active
                ) VALUES (
                    125, 2, 80, 'SEC-PNC-25', 'قسم العلاقات الدولية (الايسيسكو)',
                    'SECTION', 25, 1
                );
                INSERT INTO system_setting(key, value)
                VALUES ('ORG_APPROVED_STRUCTURE_VERSION', '2023-05-08:v1');
            """)
            conn.commit()
        finally:
            conn.close()

    def test_dry_run_finds_both_links_without_changing_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "workflow.db"
            self.make_db(db_path)
            conn = sqlite3.connect(db_path)
            try:
                conn.row_factory = sqlite3.Row
                repairs = repair.plan_repairs(conn)
                parents = dict(conn.execute(
                    "SELECT id, parent_id FROM org_nodes WHERE id IN (18, 125)"
                ).fetchall())
            finally:
                conn.close()

            self.assertEqual({item.node_id for item in repairs}, {18, 125})
            self.assertEqual(parents, {18: 17, 125: 80})

    def test_apply_repairs_links_and_creates_consistent_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "workflow.db"
            self.make_db(db_path)

            repairs, backup_path = repair.apply_repairs(db_path)

            self.assertEqual({item.node_id for item in repairs}, {18, 125})
            self.assertIsNotNone(backup_path)
            self.assertTrue(backup_path.is_file())

            conn = sqlite3.connect(db_path)
            backup_conn = sqlite3.connect(backup_path)
            try:
                parents = dict(conn.execute(
                    "SELECT id, parent_id FROM org_nodes WHERE id IN (18, 125)"
                ).fetchall())
                old_parents = dict(backup_conn.execute(
                    "SELECT id, parent_id FROM org_nodes WHERE id IN (18, 125)"
                ).fetchall())
                version = conn.execute(
                    "SELECT value FROM system_setting "
                    "WHERE key = 'ORG_APPROVED_STRUCTURE_VERSION'"
                ).fetchone()[0]
                quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                conn.close()
                backup_conn.close()

            self.assertEqual(parents, {18: 39, 125: 90})
            self.assertEqual(old_parents, {18: 17, 125: 80})
            self.assertEqual(version, repair.APPROVED_STRUCTURE_VERSION)
            self.assertEqual(quick_check, "ok")


if __name__ == "__main__":
    unittest.main()
