import unittest
from datetime import date, timedelta

from flask import Flask, g

from extensions import db
from models import (
    EmployeeFile,
    HRAttendanceScheduleDay,
    HRAttendanceSchedulePlan,
    User,
    WorkSchedule,
)
from portal.routes import (
    _administrative_affairs_schedule_change_requests,
    _attendance_schedule_effective_plan,
)


class AttendanceScheduleChangeScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="attendance-schedule-scope-test",
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
        g._attendance_schedule_cache = {}

        self.employee = User(
            email="scope-employee@example.test",
            name="موظف نطاق التغيير",
            password_hash="not-used",
            role="EMPLOYEE",
        )
        self.manager = User(
            email="scope-manager@example.test",
            name="مدير نطاق التغيير",
            password_hash="not-used",
            role="MANAGER",
        )
        self.regular = WorkSchedule(
            name="الدوام الاعتيادي",
            kind="FIXED",
            start_time="08:00",
            end_time="15:00",
            is_active=True,
        )
        self.changed = WorkSchedule(
            name="الدوام المعدل",
            kind="FIXED",
            start_time="09:00",
            end_time="17:00",
            is_active=True,
        )
        db.session.add_all((self.employee, self.manager, self.regular, self.changed))
        db.session.flush()
        db.session.add(EmployeeFile(user_id=self.employee.id, direct_manager_user_id=self.manager.id))
        db.session.commit()

    def _plan(self, *, version, status, request_type, scope, period_end="2026-09-12"):
        plan = HRAttendanceSchedulePlan(
            user_id=self.employee.id,
            manager_user_id=self.manager.id,
            period_start="2026-09-06",
            period_end=period_end,
            version_no=version,
            status=status,
            request_type=request_type,
            change_scope=scope,
        )
        db.session.add(plan)
        db.session.flush()
        for offset in range(7):
            db.session.add(HRAttendanceScheduleDay(
                plan_id=plan.id,
                work_date=(date(2026, 9, 6) + timedelta(days=offset)).isoformat(),
                day_type="WORK",
                schedule_id=self.changed.id if request_type == "CHANGE_REQUEST" else self.regular.id,
                start_time="09:00" if request_type == "CHANGE_REQUEST" else "08:00",
                end_time="17:00" if request_type == "CHANGE_REQUEST" else "15:00",
            ))
        db.session.commit()
        return plan

    def test_weekly_change_returns_to_previous_plan_after_period(self):
        baseline = self._plan(
            version=1,
            status="FINAL_APPROVED",
            request_type="BASELINE",
            scope="PERMANENT",
        )
        weekly = self._plan(
            version=2,
            status="FINAL_APPROVED",
            request_type="CHANGE_REQUEST",
            scope="WEEKLY",
        )

        during_week = _attendance_schedule_effective_plan(
            self.employee.id, "2026-09-10", final_only=True
        )
        after_week = _attendance_schedule_effective_plan(
            self.employee.id, "2026-09-13", final_only=True
        )

        self.assertEqual(during_week.id, weekly.id)
        self.assertEqual(after_week.id, baseline.id)

    def test_pending_change_never_becomes_effective_before_final_approval(self):
        baseline = self._plan(
            version=1,
            status="FINAL_APPROVED",
            request_type="BASELINE",
            scope="PERMANENT",
        )
        self._plan(
            version=2,
            status="GENERAL_DIRECTOR_APPROVED",
            request_type="CHANGE_REQUEST",
            scope="PERMANENT",
        )

        effective = _attendance_schedule_effective_plan(
            self.employee.id, "2026-09-10", final_only=True
        )

        self.assertEqual(effective.id, baseline.id)

    def test_daily_report_request_listing_respects_date_and_scope(self):
        request_plan = self._plan(
            version=1,
            status="SUBMITTED",
            request_type="CHANGE_REQUEST",
            scope="WEEKLY",
        )

        rows = _administrative_affairs_schedule_change_requests(
            date(2026, 9, 8), date(2026, 9, 10), user_ids=[self.employee.id]
        )
        outside_rows = _administrative_affairs_schedule_change_requests(
            date(2026, 9, 13), date(2026, 9, 14), user_ids=[self.employee.id]
        )

        self.assertEqual([row.id for row in rows], [request_plan.id])
        self.assertEqual(outside_rows, [])
        self.assertEqual(rows[0].change_scope, "WEEKLY")


if __name__ == "__main__":
    unittest.main()
