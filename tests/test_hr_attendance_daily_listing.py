import unittest
from datetime import datetime
from types import SimpleNamespace

from flask import Flask
from flask_login import LoginManager, login_user, logout_user

from extensions import db
from models import User
from portal.routes import (
    _attendance_count_day,
    _attendance_event_date_range,
    _hr_can_edit_attendance,
    _sort_and_number_attendance_daily_rows,
)


class AttendanceDailyListingTests(unittest.TestCase):
    def test_attendance_counter_uses_the_selected_day_for_a_single_day_filter(self):
        self.assertEqual(
            _attendance_count_day("2026-08-30", "2026-08-30", "2026-09-01"),
            "2026-08-30",
        )

    def test_attendance_counter_uses_today_for_a_multi_day_filter(self):
        self.assertEqual(
            _attendance_count_day("2026-08-30", "2026-09-01", "2026-09-01"),
            "2026-09-01",
        )

    def test_attendance_events_defaults_to_today_and_retains_selected_dates(self):
        today = "2026-09-01"
        self.assertEqual(_attendance_event_date_range("", "", today), (today, today))
        self.assertEqual(
            _attendance_event_date_range("2026-08-30", "", today),
            ("2026-08-30", "2026-08-30"),
        )
        self.assertEqual(
            _attendance_event_date_range("2026-01-01", "2026-09-01", today),
            ("2026-01-01", "2026-09-01"),
        )

    def test_rows_are_numbered_by_first_checkin_within_each_day(self):
        first = SimpleNamespace(user_id=1, day="2026-09-01", first_in=datetime(2026, 9, 1, 8, 0))
        second = SimpleNamespace(user_id=2, day="2026-09-01", first_in=datetime(2026, 9, 1, 8, 15))
        missing = SimpleNamespace(user_id=3, day="2026-09-01", first_in=None)
        next_day = SimpleNamespace(user_id=4, day="2026-09-02", first_in=datetime(2026, 9, 2, 8, 5))

        rows = _sort_and_number_attendance_daily_rows([second, missing, next_day, first])

        self.assertEqual(rows, [next_day, first, second, missing])
        self.assertEqual(next_day.daily_employee_number, 1)
        self.assertEqual(first.daily_employee_number, 1)
        self.assertEqual(second.daily_employee_number, 2)
        self.assertIsNone(missing.daily_employee_number)


class AttendanceManualEditPermissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            SECRET_KEY="attendance-editor-permission-test",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(cls.app)
        LoginManager(cls.app)
        cls.context = cls.app.app_context()
        cls.context.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.context.pop()

    def setUp(self):
        db.session.remove()
        db.drop_all()
        db.create_all()

    def test_only_secretary_general_is_an_attendance_editor(self):
        hr_user = User(email="hr@example.test", name="HR", password_hash="x", role="HR")
        secretary_general = User(
            email="secretary@example.test",
            name="Secretary General",
            password_hash="x",
            role="GENERAL-SECRETARY",
        )
        db.session.add_all((hr_user, secretary_general))
        db.session.commit()

        with self.app.test_request_context():
            login_user(hr_user)
            self.assertFalse(_hr_can_edit_attendance())
            logout_user()

            login_user(secretary_general)
            self.assertTrue(_hr_can_edit_attendance())
            logout_user()


if __name__ == "__main__":
    unittest.main()
