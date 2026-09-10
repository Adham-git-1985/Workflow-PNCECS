import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from flask import Flask
from jinja2 import Environment

from extensions import db
from models import (
    EmployeeFile,
    HRAttendanceScheduleDay,
    HRAttendanceSchedulePlan,
    Notification,
    SystemSetting,
    User,
    WorkSchedule,
)
from portal.routes import (
    _attendance_exemption_reason,
    _attendance_schedule_fork_plan,
    _attendance_schedule_latest_plan,
    _effective_schedule_for_user,
)
from services.attendance_schedule import (
    attendance_schedule_cycle_start,
    attendance_schedule_needs_reminder,
    send_attendance_schedule_reminders,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class AttendanceScheduleCycleTests(unittest.TestCase):
    def test_second_week_keeps_the_same_cycle(self):
        self.assertEqual(
            attendance_schedule_cycle_start(date(2026, 9, 10)),
            date(2026, 9, 6),
        )
        self.assertEqual(
            attendance_schedule_cycle_start(date(2026, 9, 19)),
            date(2026, 9, 6),
        )
        self.assertEqual(
            attendance_schedule_cycle_start(date(2026, 9, 20)),
            date(2026, 9, 20),
        )

    def test_reminder_opens_three_days_before_second_week(self):
        period_start = date(2026, 9, 6)
        self.assertFalse(attendance_schedule_needs_reminder(None, period_start, date(2026, 9, 9)))
        self.assertTrue(attendance_schedule_needs_reminder(None, period_start, date(2026, 9, 10)))
        self.assertTrue(attendance_schedule_needs_reminder(None, period_start, date(2026, 9, 13)))
        completed = SimpleNamespace(status="SUBMITTED", days=[object()] * 14)
        self.assertFalse(attendance_schedule_needs_reminder(completed, period_start, date(2026, 9, 10)))


class AttendanceSchedulePersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="attendance-schedule-test",
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
        self.employee = User(
            email="schedule-employee@example.test",
            name="موظف الجدول",
            password_hash="not-used",
            role="EMPLOYEE",
        )
        self.manager = User(
            email="schedule-manager@example.test",
            name="مدير الجدول",
            password_hash="not-used",
            role="MANAGER",
        )
        self.schedule = WorkSchedule(
            name="الدوام الاعتيادي",
            kind="FIXED",
            start_time="08:00",
            end_time="15:00",
            is_active=True,
        )
        db.session.add_all((self.employee, self.manager, self.schedule))
        db.session.flush()
        db.session.add_all((
            EmployeeFile(
                user_id=self.employee.id,
                direct_manager_user_id=self.manager.id,
            ),
            SystemSetting(key="HR_WEEKLY_HOLIDAYS_MASK", value="48"),
            SystemSetting(key="HR_DEFAULT_SCHEDULE_ID", value=str(self.schedule.id)),
        ))
        db.session.commit()

    def _final_plan(self):
        plan = HRAttendanceSchedulePlan(
            user_id=self.employee.id,
            manager_user_id=self.manager.id,
            period_start="2026-09-06",
            period_end="2026-09-19",
            version_no=1,
            status="FINAL_APPROVED",
        )
        db.session.add(plan)
        db.session.flush()
        db.session.add_all((
            HRAttendanceScheduleDay(
                plan_id=plan.id,
                work_date="2026-09-10",
                day_type="WORK",
                schedule_id=self.schedule.id,
                start_time="09:00",
                end_time="17:00",
            ),
            HRAttendanceScheduleDay(
                plan_id=plan.id,
                work_date="2026-09-11",
                day_type="OFF",
            ),
        ))
        db.session.commit()
        return plan

    def test_final_plan_overrides_default_attendance_schedule(self):
        self._final_plan()

        effective = _effective_schedule_for_user(self.employee.id, "2026-09-10")

        self.assertEqual(effective.start_time, "09:00")
        self.assertEqual(effective.end_time, "17:00")
        self.assertIsNone(_effective_schedule_for_user(self.employee.id, "2026-09-11"))
        self.assertEqual(
            _attendance_exemption_reason(self.employee.id, "2026-09-11"),
            "PLANNED_OFF",
        )

    def test_editing_final_plan_creates_a_new_version(self):
        final_plan = self._final_plan()

        draft = _attendance_schedule_fork_plan(
            final_plan,
            self.employee.id,
            "EMPLOYEE",
        )
        db.session.commit()

        self.assertEqual(draft.version_no, 2)
        self.assertEqual(draft.status, "DRAFT")
        self.assertEqual(draft.replaces_plan_id, final_plan.id)
        self.assertEqual(len(draft.days), 2)
        self.assertEqual(
            _attendance_schedule_latest_plan(
                self.employee.id,
                "2026-09-06",
                final_only=True,
            ).id,
            final_plan.id,
        )

    def test_automatic_reminder_is_deduplicated(self):
        first = send_attendance_schedule_reminders(date(2026, 9, 10))
        second = send_attendance_schedule_reminders(date(2026, 9, 10))
        db.session.commit()

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(Notification.query.filter_by(user_id=self.employee.id).count(), 1)


class AttendanceScheduleTemplateTests(unittest.TestCase):
    def test_template_and_navigation_expose_the_full_workflow(self):
        template_path = PROJECT_ROOT / "templates" / "portal" / "hr" / "work_schedule.html"
        template = template_path.read_text(encoding="utf-8")
        navigation = (
            PROJECT_ROOT / "templates" / "portal" / "hr" / "_module_nav.html"
        ).read_text(encoding="utf-8")

        Environment().parse(template)
        for token in (
            "اقتراح الموظف",
            "اعتماد المدير",
            "الاعتماد النهائي",
            "اعتماد الجاهز دفعة واحدة",
            "عرض إداري فقط",
            "data-copy-first-week",
        ):
            with self.subTest(token=token):
                self.assertIn(token, template)
        self.assertIn("portal.hr_work_schedule", navigation)
        self.assertIn("جدول الدوام", navigation)


if __name__ == "__main__":
    unittest.main()
