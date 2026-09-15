import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask
from sqlalchemy import text

from extensions import db
from portal import hr_alerts_job


class AttendanceRuntimeSchemaTests(unittest.TestCase):
    def test_legacy_sqlite_tables_receive_j3c4_columns_and_indexes(self):
        with patch.dict(os.environ, {"SKIP_RUNTIME_SCHEMA": "1"}):
            app_module = importlib.import_module("app")

        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "legacy.sqlite"
            test_app = Flask(__name__)
            test_app.config.update(
                TESTING=True,
                SQLALCHEMY_DATABASE_URI=f"sqlite:///{database_path.as_posix()}",
                SQLALCHEMY_TRACK_MODIFICATIONS=False,
            )
            db.init_app(test_app)

            with test_app.app_context():
                db.create_all()
                db.session.execute(text("DROP TABLE hr_leave_request"))
                db.session.execute(text("DROP TABLE hr_att_special_case"))
                db.session.execute(text(
                    "CREATE TABLE hr_leave_request ("
                    "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL)"
                ))
                db.session.execute(text(
                    "CREATE TABLE hr_att_special_case ("
                    "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL)"
                ))
                db.session.commit()

                with patch.object(app_module, "app", test_app):
                    app_module._ensure_runtime_schema()
                    # A second startup must be harmless and must not duplicate
                    # any of the indexes.
                    app_module._ensure_runtime_schema()

                leave_columns = {
                    row[1]
                    for row in db.session.execute(
                        text("PRAGMA table_info(hr_leave_request)")
                    ).all()
                }
                special_case_columns = {
                    row[1]
                    for row in db.session.execute(
                        text("PRAGMA table_info(hr_att_special_case)")
                    ).all()
                }
                self.assertTrue({
                    "source",
                    "source_attendance_day",
                    "replaced_by_leave_request_id",
                    "replaced_at",
                    "replacement_reason",
                }.issubset(leave_columns))
                self.assertTrue({
                    "final_approved_by_id",
                    "final_approved_at",
                    "final_approval_note",
                }.issubset(special_case_columns))

                leave_indexes = {
                    row[1]: int(row[2] or 0)
                    for row in db.session.execute(
                        text("PRAGMA index_list(hr_leave_request)")
                    ).all()
                }
                special_case_indexes = {
                    row[1]
                    for row in db.session.execute(
                        text("PRAGMA index_list(hr_att_special_case)")
                    ).all()
                }
                self.assertTrue({
                    "ix_hr_leave_request_source",
                    "ix_hr_leave_request_source_attendance_day",
                    "ix_hr_leave_request_replaced_by_leave_request_id",
                    "ix_hr_leave_request_replaced_at",
                    "uq_hr_leave_attendance_auto_day",
                }.issubset(leave_indexes))
                self.assertEqual(
                    leave_indexes["uq_hr_leave_attendance_auto_day"],
                    1,
                )
                self.assertTrue({
                    "ix_hr_att_special_case_final_approved_by_id",
                    "ix_hr_att_special_case_final_approved_at",
                }.issubset(special_case_indexes))

                unique_columns = [
                    row[2]
                    for row in db.session.execute(
                        text("PRAGMA index_info(uq_hr_leave_attendance_auto_day)")
                    ).all()
                ]
                self.assertEqual(
                    unique_columns,
                    ["user_id", "source", "source_attendance_day"],
                )

                db.session.remove()
                db.drop_all()
                db.session.remove()
                db.engine.dispose()


class HRAlertsJobAtomicityTests(unittest.TestCase):
    def test_attendance_reconciliation_failure_rolls_back_and_propagates(self):
        session = Mock()
        reconciliation_error = RuntimeError("attendance reconciliation failed")

        with (
            patch.object(
                hr_alerts_job,
                "process_pending_approvals",
                return_value={"reminded": 1, "escalated": 0},
            ),
            patch.object(hr_alerts_job, "_setting_get", return_value="0"),
            patch.object(
                hr_alerts_job,
                "send_attendance_schedule_reminders",
                return_value=0,
            ),
            patch.object(hr_alerts_job.db, "session", session),
            patch(
                "portal.routes._process_unrecorded_office_attendance",
                side_effect=reconciliation_error,
            ),
        ):
            with self.assertRaises(RuntimeError) as raised:
                hr_alerts_job._check_pending_leave_requests()

        self.assertIs(raised.exception, reconciliation_error)
        session.rollback.assert_called_once_with()
        session.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
