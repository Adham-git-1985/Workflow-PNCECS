import unittest
from datetime import date, datetime
from pathlib import Path

from flask import Flask
from jinja2 import Environment

from extensions import db
from models import (
    AttendanceDailySummary,
    EmployeeFile,
    HRLeaveRequest,
    HRLeaveType,
    SystemSetting,
    User,
    WorkAssignment,
    WorkPolicy,
    WorkSchedule,
)
from portal.routes import _my_attendance_month_bounds, _my_attendance_month_rows


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class MyAttendanceMonthBoundsTests(unittest.TestCase):
    def test_defaults_to_previous_month_across_year_boundary(self):
        month, month_start, month_end = _my_attendance_month_bounds(
            None,
            reference_day=date(2026, 1, 10),
        )

        self.assertEqual(month, "2025-12")
        self.assertEqual(month_start, date(2025, 12, 1))
        self.assertEqual(month_end, date(2025, 12, 31))

    def test_uses_selected_month_and_rejects_invalid_values(self):
        selected = _my_attendance_month_bounds("2026-02", reference_day=date(2026, 9, 8))
        fallback = _my_attendance_month_bounds("2026-13", reference_day=date(2026, 9, 8))
        out_of_range = _my_attendance_month_bounds("9999-12", reference_day=date(2026, 9, 8))

        self.assertEqual(selected, ("2026-02", date(2026, 2, 1), date(2026, 2, 28)))
        self.assertEqual(fallback, ("2026-08", date(2026, 8, 1), date(2026, 8, 31)))
        self.assertEqual(out_of_range, fallback)


class MyAttendanceMonthlyRowsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="my-attendance-month-test",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(cls.app)
        cls.context = cls.app.app_context()
        cls.context.push()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.context.pop()

    def setUp(self):
        db.session.remove()
        db.drop_all()
        db.create_all()

        self.user = User(
            email="monthly-attendance@example.test",
            name="Monthly Attendance",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        self.schedule = WorkSchedule(
            name="Regular",
            kind="FIXED",
            start_time="08:00",
            end_time="15:00",
            is_active=True,
        )
        self.regular_policy = WorkPolicy(
            name="Regular policy",
            days_policy="FIXED",
            fixed_days_mask=79,
            location_policy="ONSITE",
            is_active=True,
        )
        db.session.add_all((self.user, self.schedule, self.regular_policy))
        db.session.flush()
        self.employee_file = EmployeeFile(
            user_id=self.user.id,
            timeclock_code="1001",
            hire_date="2026-03-02",
        )
        db.session.add_all((
            self.employee_file,
            SystemSetting(key="HR_WEEKLY_HOLIDAYS_MASK", value="48"),
            SystemSetting(key="HR_DEFAULT_SCHEDULE_ID", value=str(self.schedule.id)),
            SystemSetting(key="HR_REGULAR_POLICY_ID", value=str(self.regular_policy.id)),
        ))
        db.session.commit()

    def test_builds_missing_absence_and_preserves_attendance_and_leave(self):
        leave_type = HRLeaveType(code="ANNUAL", name_ar="سنوية", is_active=True)
        db.session.add(leave_type)
        db.session.flush()
        db.session.add_all((
            AttendanceDailySummary(
                user_id=self.user.id,
                day="2026-03-02",
                first_in=datetime(2026, 3, 2, 8, 0),
                last_out=datetime(2026, 3, 2, 15, 0),
                work_minutes=420,
                status="OK",
            ),
            AttendanceDailySummary(
                user_id=self.user.id,
                day="2026-03-03",
                first_in=datetime(2026, 3, 3, 8, 0),
                status="INCOMPLETE",
            ),
            HRLeaveRequest(
                user_id=self.user.id,
                leave_type_id=leave_type.id,
                start_date="2026-03-05",
                end_date="2026-03-05",
                status="APPROVED",
            ),
        ))
        db.session.commit()

        rows, stats = _my_attendance_month_rows(
            self.user.id,
            self.employee_file,
            date(2026, 3, 1),
            date(2026, 3, 31),
            reference_day=date(2026, 3, 7),
        )
        status_by_day = {row.day: row.status for row in rows}

        self.assertEqual(status_by_day, {
            "2026-03-05": "APPROVED_LEAVE",
            "2026-03-04": "ABSENT",
            "2026-03-03": "INCOMPLETE",
            "2026-03-02": "OK",
        })
        self.assertEqual(stats, {
            "attendance_days": 2,
            "absence_days": 1,
            "incomplete_days": 1,
            "remote_days": 0,
        })
        self.assertEqual(AttendanceDailySummary.query.count(), 2)

    def test_does_not_mark_the_current_day_absent_before_it_ends(self):
        db.session.add(AttendanceDailySummary(
            user_id=self.user.id,
            day="2026-03-04",
            status="ABSENT",
        ))
        db.session.commit()

        rows, stats = _my_attendance_month_rows(
            self.user.id,
            self.employee_file,
            date(2026, 3, 1),
            date(2026, 3, 31),
            reference_day=date(2026, 3, 4),
        )

        self.assertNotIn("2026-03-04", {row.day for row in rows})
        self.assertEqual(stats["absence_days"], 2)

    def test_flexible_hybrid_policy_marks_only_the_weekly_quota_deficit_absent(self):
        hybrid_policy = WorkPolicy(
            name="Hybrid policy",
            days_policy="HYBRID_WEEKLY_QUOTA",
            hybrid_office_days=3,
            hybrid_remote_days=2,
            hybrid_selection_mode="FLEXIBLE",
            location_policy="HYBRID",
            is_active=True,
        )
        db.session.add(hybrid_policy)
        db.session.flush()
        db.session.add(WorkAssignment(
            name="Hybrid employee",
            schedule_id=self.schedule.id,
            policy_id=hybrid_policy.id,
            target_type="USER",
            target_user_id=self.user.id,
            is_active=True,
        ))
        db.session.add_all((
            AttendanceDailySummary(
                user_id=self.user.id,
                day="2026-03-02",
                first_in=datetime(2026, 3, 2, 8, 0),
                last_out=datetime(2026, 3, 2, 15, 0),
                status="OK",
            ),
            AttendanceDailySummary(
                user_id=self.user.id,
                day="2026-03-03",
                first_in=datetime(2026, 3, 3, 8, 0),
                last_out=datetime(2026, 3, 3, 15, 0),
                status="OK",
            ),
        ))
        db.session.commit()

        rows, stats = _my_attendance_month_rows(
            self.user.id,
            self.employee_file,
            date(2026, 3, 1),
            date(2026, 3, 31),
            reference_day=date(2026, 3, 9),
        )
        first_week_rows = [row for row in rows if "2026-03-02" <= row.day <= "2026-03-08"]

        self.assertEqual(len(first_week_rows), 5)
        self.assertEqual(stats["attendance_days"], 2)
        self.assertEqual(stats["absence_days"], 1)
        self.assertEqual(stats["remote_days"], 2)


class MyAttendanceTemplateTests(unittest.TestCase):
    def test_template_exposes_month_filter_and_monthly_counters(self):
        template = (
            PROJECT_ROOT / "templates" / "portal" / "hr" / "my_attendance.html"
        ).read_text(encoding="utf-8")

        Environment().parse(template)
        self.assertIn('type="month"', template)
        self.assertIn("attendance_stats.attendance_days", template)
        self.assertIn("attendance_stats.absence_days", template)
        self.assertIn("دوام عن بُعد / هجين", template)


if __name__ == "__main__":
    unittest.main()
