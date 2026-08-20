import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile

from admin.routes import (
    _inspect_sqlite_database,
    _make_restore_tempdir,
    _restore_sqlite_from_snapshot,
    _safe_extract_backup_zip,
    _validate_files_manifest,
    _validate_sqlite_snapshot,
)


class BackupRestoreTests(unittest.TestCase):
    @staticmethod
    def make_database(path, rows_by_table):
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
            connection.execute("INSERT INTO users (name) VALUES ('Admin')")
            for table_name, row_count in rows_by_table.items():
                connection.execute(f'CREATE TABLE "{table_name}" (id INTEGER PRIMARY KEY, value TEXT)')
                connection.executemany(
                    f'INSERT INTO "{table_name}" (value) VALUES (?)',
                    [(f"row-{number}",) for number in range(row_count)],
                )
            connection.commit()
        finally:
            connection.close()

    def test_restore_tempdir_uses_short_system_temp_path(self):
        restore_dir = _make_restore_tempdir("wf_restore_test")
        try:
            self.assertTrue(os.path.isdir(restore_dir.name))
            self.assertTrue(
                os.path.commonpath([tempfile.gettempdir(), restore_dir.name])
                == os.path.abspath(tempfile.gettempdir())
            )
        finally:
            restore_dir.cleanup()

    def test_safe_extract_accepts_backup_layout(self):
        restore_dir = _make_restore_tempdir("wf_restore_test")
        archive_path = os.path.join(restore_dir.name, "sample.zip")
        extract_path = os.path.join(restore_dir.name, "out")
        try:
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("backup_meta.json", "{}")
                archive.writestr("db/workflow.db", b"SQLite format 3\x00")
                archive.writestr("instance/uploads/meeting_letterheads/letterhead.docx", b"doc")
            with zipfile.ZipFile(archive_path, "r") as archive:
                _safe_extract_backup_zip(archive, extract_path)
            self.assertTrue(os.path.isfile(os.path.join(extract_path, "db", "workflow.db")))
        finally:
            restore_dir.cleanup()

    def test_safe_extract_rejects_path_traversal(self):
        restore_dir = _make_restore_tempdir("wf_restore_test")
        archive_path = os.path.join(restore_dir.name, "unsafe.zip")
        try:
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../outside.txt", "unsafe")
            with zipfile.ZipFile(archive_path, "r") as archive:
                with self.assertRaises(zipfile.BadZipFile):
                    _safe_extract_backup_zip(archive, os.path.join(restore_dir.name, "out"))
        finally:
            restore_dir.cleanup()

    def test_database_manifest_includes_every_table_and_row(self):
        with tempfile.TemporaryDirectory() as root:
            database_path = os.path.join(root, "workflow.db")
            self.make_database(database_path, {
                "trouble_tickets": 2,
                "transport_permit": 3,
                "inv_item": 4,
                "portal_permission_preset": 5,
            })
            manifest = _inspect_sqlite_database(database_path)
            self.assertEqual(manifest["table_count"], 5)
            self.assertEqual(manifest["total_rows"], 15)
            self.assertEqual(manifest["tables"]["transport_permit"]["row_count"], 3)
            self.assertEqual(
                [column["name"] for column in manifest["tables"]["inv_item"]["columns"]],
                ["id", "value"],
            )

    def test_snapshot_validation_detects_count_and_checksum_changes(self):
        with tempfile.TemporaryDirectory() as root:
            database_path = os.path.join(root, "workflow.db")
            self.make_database(database_path, {"trouble_tickets": 2})
            manifest = _inspect_sqlite_database(database_path)
            with open(database_path, "rb") as database_file:
                manifest["sha256"] = hashlib.sha256(database_file.read()).hexdigest()
            self.assertEqual(_validate_sqlite_snapshot(database_path, manifest)["table_count"], 2)

            changed_manifest = json.loads(json.dumps(manifest))
            changed_manifest["tables"]["trouble_tickets"]["row_count"] = 99
            with self.assertRaises(ValueError):
                _validate_sqlite_snapshot(database_path, changed_manifest)

            changed_manifest = json.loads(json.dumps(manifest))
            changed_manifest["sha256"] = "0" * 64
            with self.assertRaises(ValueError):
                _validate_sqlite_snapshot(database_path, changed_manifest)
            validated = _validate_sqlite_snapshot(
                database_path,
                changed_manifest,
                verify_checksum=False,
            )
            self.assertEqual(validated["tables"]["trouble_tickets"]["row_count"], 2)

    def test_restore_round_trip_preserves_new_module_tables(self):
        with tempfile.TemporaryDirectory() as root:
            source_path = os.path.join(root, "source.db")
            destination_path = os.path.join(root, "destination.db")
            self.make_database(source_path, {
                "trouble_tickets": 2,
                "transport_permit": 3,
                "inv_employee_request": 4,
                "portal_permission_preset": 5,
            })
            self.make_database(destination_path, {"old_data": 1})
            manifest = _inspect_sqlite_database(source_path)
            _restore_sqlite_from_snapshot(source_path, destination_path)
            restored = _validate_sqlite_snapshot(destination_path, manifest)
            self.assertNotIn("old_data", restored["tables"])
            self.assertEqual(restored["tables"]["inv_employee_request"]["row_count"], 4)

    def test_file_manifest_detects_missing_or_changed_attachments(self):
        with tempfile.TemporaryDirectory() as root:
            attachment_path = os.path.join(root, "instance", "uploads", "trouble_tickets", "ticket.jpg")
            os.makedirs(os.path.dirname(attachment_path), exist_ok=True)
            content = b"ticket-attachment"
            with open(attachment_path, "wb") as attachment:
                attachment.write(content)
            manifest = {
                "files": [{
                    "path": "instance/uploads/trouble_tickets/ticket.jpg",
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }],
            }
            _validate_files_manifest(root, manifest)
            with open(attachment_path, "wb") as attachment:
                attachment.write(b"changed")
            with self.assertRaises(ValueError):
                _validate_files_manifest(root, manifest)


if __name__ == "__main__":
    unittest.main()
