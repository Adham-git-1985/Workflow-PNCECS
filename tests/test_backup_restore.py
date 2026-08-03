import os
import tempfile
import unittest
import zipfile

from admin.routes import _make_restore_tempdir, _safe_extract_backup_zip


class BackupRestoreTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
