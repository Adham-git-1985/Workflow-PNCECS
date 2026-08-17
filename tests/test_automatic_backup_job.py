import json
import os
import tempfile
import unittest
import zipfile
from datetime import date, datetime
from unittest.mock import patch

from flask import Flask

from jobs.backup_job import (
    backup_destination_dir,
    backup_is_due,
    create_automatic_backup,
    latest_due_date,
    start_automatic_backup_job,
)


class AutomaticBackupJobTests(unittest.TestCase):
    def test_latest_due_date_before_and_after_three_pm(self):
        self.assertEqual(
            latest_due_date(datetime(2026, 8, 17, 14, 59)),
            date(2026, 8, 16),
        )
        self.assertEqual(
            latest_due_date(datetime(2026, 8, 17, 15, 0)),
            date(2026, 8, 17),
        )

    def test_backup_is_due_only_for_a_missing_schedule_date(self):
        scheduled_for = date(2026, 8, 17)
        self.assertTrue(backup_is_due(None, scheduled_for))
        self.assertTrue(backup_is_due(date(2026, 8, 16), scheduled_for))
        self.assertFalse(backup_is_due(date(2026, 8, 17), scheduled_for))

    def test_configured_destination_is_used(self):
        with tempfile.TemporaryDirectory() as root:
            app = Flask(__name__, instance_path=os.path.join(root, "instance"))
            configured = os.path.join(root, "desktop backups")
            app.config["AUTO_BACKUP_DIR"] = configured
            self.assertEqual(backup_destination_dir(app), os.path.abspath(configured))

    def test_default_destination_uses_detected_desktop_without_a_fixed_device_path(self):
        with tempfile.TemporaryDirectory() as root:
            app = Flask(__name__, instance_path=os.path.join(root, "instance"))
            app.config["AUTO_BACKUP_DIR"] = ""
            detected_desktop = os.path.join(root, "Desktop")
            with patch(
                "jobs.backup_job._windows_desktop_path",
                return_value=detected_desktop,
            ):
                self.assertEqual(
                    backup_destination_dir(app),
                    os.path.abspath(detected_desktop),
                )

    def test_job_does_not_start_for_testing_apps(self):
        app = Flask(__name__)
        app.config["TESTING"] = True
        self.assertIsNone(start_automatic_backup_job(app))

    def test_creates_desktop_copy_and_persists_success_state(self):
        with tempfile.TemporaryDirectory() as root:
            instance_path = os.path.join(root, "instance")
            runtime_dir = os.path.join(instance_path, "tmp", "generated")
            destination_dir = os.path.join(root, "Desktop")
            os.makedirs(runtime_dir, exist_ok=True)
            source_path = os.path.join(runtime_dir, "workflow_backup_20260817_030000.zip")
            with zipfile.ZipFile(source_path, "w") as archive:
                archive.writestr("backup_meta.json", "{}")

            app = Flask(__name__, instance_path=instance_path)
            app.config["AUTO_BACKUP_DIR"] = destination_dir
            scheduled_for = date(2026, 8, 17)
            destination_path = create_automatic_backup(
                app,
                scheduled_for,
                completed_at=datetime(2026, 8, 17, 15, 0),
                build_backup=lambda: source_path,
            )

            self.assertTrue(os.path.isfile(destination_path))
            self.assertEqual(os.path.dirname(destination_path), destination_dir)
            self.assertFalse(os.path.exists(runtime_dir))

            state_path = os.path.join(instance_path, "automatic_backup_state.json")
            with open(state_path, "r", encoding="utf-8") as state_file:
                state = json.load(state_file)
            self.assertEqual(state["last_successful_schedule_date"], "2026-08-17")
            self.assertEqual(state["backup_path"], os.path.abspath(destination_path))


if __name__ == "__main__":
    unittest.main()
