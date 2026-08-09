import importlib.util
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "repair_workflow_db.py"
spec = importlib.util.spec_from_file_location("repair_workflow_db", MODULE_PATH)
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


class RepairWorkflowDbTests(unittest.TestCase):
    def make_db(self, path: Path, value: str = "ok"):
        conn = sqlite3.connect(path)
        try:
            conn.execute("CREATE TABLE marker(value TEXT)")
            conn.execute("INSERT INTO marker(value) VALUES (?)", (value,))
            conn.commit()
        finally:
            conn.close()

    def test_validate_rejects_non_sqlite_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "workflow.db"
            bad.write_text("not a database", encoding="utf-8")
            valid, reason = repair.validate_sqlite(bad)
            self.assertFalse(valid)
            self.assertTrue(reason)

    def test_finds_valid_database_inside_backup_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "good.db"
            self.make_db(db)
            archive = root / "workflow_backup_1.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.write(db, "db/workflow.db")
            work = root / "work"
            work.mkdir()
            candidate, reason = repair.extract_valid_db_from_zip(archive, work)
            self.assertEqual(reason, "ok")
            self.assertIsNotNone(candidate)
            self.assertTrue(repair.validate_sqlite(candidate)[0])

    def test_restore_preserves_invalid_db_and_replaces_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "instance" / "workflow.db"
            dest.parent.mkdir()
            dest.write_text("bad database bytes", encoding="utf-8")
            Path(str(dest) + "-wal").write_bytes(b"stale wal")
            candidate = root / "candidate.db"
            self.make_db(candidate, "restored")

            preserved = repair.restore_database(dest, candidate, "test")

            self.assertTrue(repair.validate_sqlite(dest)[0])
            self.assertGreaterEqual(len(preserved), 2)
            conn = sqlite3.connect(dest)
            try:
                value = conn.execute("SELECT value FROM marker").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(value, "restored")


if __name__ == "__main__":
    unittest.main()
