import inspect
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from flask import Flask

from extensions import db
from models import (
    EmployeeFile,
    HRAttendanceSchedulePlan,
    SystemSetting,
    User,
    WorkSchedule,
)
from portal.routes import _effective_schedule_for_user, hr_work_schedule_update
from services.attendance_schedule import (
    attendance_schedule_cycle_days,
    attendance_schedule_cycle_start,
)


class WeeklyScheduleWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="weekly-schedule-test",
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
            email="weekly-employee@example.test",
            name="الموظف الأسبوعي",
            password_hash="x",
            role="EMPLOYEE",
        )
        self.manager = User(
            email="weekly-manager@example.test",
            name="مدير الموظف",
            password_hash="x",
            role="MANAGER",
        )
        self.hr = User(
            email="weekly-hr@example.test",
            name="الشؤون البشرية",
            password_hash="x",
            role="HR",
        )
        self.admin = User(
            email="weekly-admin@example.test",
            name="Admin",
            password_hash="x",
            role="ADMIN",
        )
        self.super_admin = User(
            email="weekly-super-admin@example.test",
            name="Super Admin",
            password_hash="x",
            role="SUPER_ADMIN",
        )
        self.secretary = User(
            email="weekly-secretary@example.test",
            name="الأمين العام",
            password_hash="x",
            role="GENERAL_SECRETARY",
        )
        self.schedule = WorkSchedule(
            name="دوام مكتبي",
            kind="FIXED",
            start_time="08:00",
            end_time="15:00",
            is_active=True,
        )
        db.session.add_all((
            self.employee,
            self.manager,
            self.hr,
            self.admin,
            self.super_admin,
            self.secretary,
            self.schedule,
        ))
        db.session.flush()
        db.session.add_all((
            EmployeeFile(user_id=self.employee.id, direct_manager_user_id=self.manager.id),
            SystemSetting(key="HR_WEEKLY_HOLIDAYS_MASK", value="0"),
            SystemSetting(key="HR_DEFAULT_SCHEDULE_ID", value=str(self.schedule.id)),
        ))
        db.session.commit()

    def _post(self, actor, data):
        fn = inspect.unwrap(hr_work_schedule_update)
        with self.app.test_request_context(
            "/portal/hr/attendance/work-schedule/update",
            method="POST",
            data=data,
        ):
            with patch("portal.routes.current_user", actor), patch(
                "portal.routes.url_for",
                return_value="/portal/hr/attendance/work-schedule",
            ), patch("portal.routes.notify_attendance_schedule_stakeholders"), patch(
                "portal.routes._portal_audit"
            ):
                return fn()

    def _week_form(self, start):
        values = {}
        for offset in range(7):
            day = start + timedelta(days=offset)
            key = day.strftime("%Y_%m_%d")
            values.update({
                f"day_type_{key}": "WORK",
                f"schedule_id_{key}": str(self.schedule.id),
                f"start_time_{key}": "08:00",
                f"end_time_{key}": "15:00",
                f"note_{key}": "",
            })
        return values

    def _future_period_start(self):
        return attendance_schedule_cycle_start(date.today() + timedelta(days=14))

    def test_date_selects_one_sunday_to_saturday_week(self):
        self.assertEqual(attendance_schedule_cycle_start(date(2026, 9, 10)), date(2026, 9, 6))
        self.assertEqual(attendance_schedule_cycle_start(date(2026, 9, 19)), date(2026, 9, 13))
        self.assertEqual(len(attendance_schedule_cycle_days(date(2026, 9, 6))), 7)

    def test_hr_publishes_baseline_and_employee_request_is_not_effective_before_final_approval(self):
        period_start = self._future_period_start()
        hr_data = {
            "target_user_id": str(self.employee.id),
            "period_start": period_start.isoformat(),
            "action": "hr_publish",
            **self._week_form(period_start),
        }
        self._post(self.hr, hr_data)
        baseline = HRAttendanceSchedulePlan.query.filter_by(user_id=self.employee.id).one()
        self.assertEqual(baseline.status, "ADMIN_APPROVED")
        self.assertEqual(baseline.request_type, "BASELINE")
        self.assertEqual(len(baseline.days), 7)
        self.assertEqual(_effective_schedule_for_user(self.employee.id, "2026-09-07").start_time, "08:00")

        request = self._post(self.employee, {
            "target_user_id": str(self.employee.id),
            "period_start": period_start.isoformat(),
            "plan_id": str(baseline.id),
            "action": "request_change",
            "employee_note": "أطلب تغيير دوامي يوم الاثنين إلى دوام عن بعد.",
        })
        self.assertEqual(request.status_code, 302)
        change = HRAttendanceSchedulePlan.query.order_by(HRAttendanceSchedulePlan.id.desc()).first()
        self.assertEqual(change.request_type, "CHANGE_REQUEST")
        self.assertEqual(change.status, "SUBMITTED")
        self.assertEqual(_effective_schedule_for_user(self.employee.id, "2026-09-07").start_time, "08:00")

        self._post(self.manager, {
            "target_user_id": str(self.employee.id),
            "period_start": period_start.isoformat(),
            "plan_id": str(change.id),
            "action": "manager_approve",
        })
        db.session.refresh(change)
        self.assertEqual(change.status, "MANAGER_APPROVED")

        self._post(self.secretary, {
            "target_user_id": str(self.employee.id),
            "period_start": period_start.isoformat(),
            "plan_id": str(change.id),
            "action": "final_approve",
        })
        db.session.refresh(change)
        self.assertEqual(change.status, "FINAL_APPROVED")

    def test_only_admin_and_super_admin_can_edit_past_days(self):
        today = date(2031, 6, 9)
        period_start = attendance_schedule_cycle_start(today - timedelta(days=7))
        past_key = period_start.strftime("%Y_%m_%d")
        data = {
            "target_user_id": str(self.employee.id),
            "period_start": period_start.isoformat(),
            "action": "hr_publish",
            **self._week_form(period_start),
        }
        data[f"start_time_{past_key}"] = "09:00"

        with patch("portal.routes._attendance_schedule_today", return_value=today):
            blocked = self._post(self.hr, data)
        self.assertEqual(blocked.status_code, 302)
        self.assertEqual(HRAttendanceSchedulePlan.query.count(), 0)

        with patch("portal.routes._attendance_schedule_today", return_value=today):
            allowed_for_admin = self._post(self.admin, data)
        self.assertEqual(allowed_for_admin.status_code, 302)
        admin_plan = HRAttendanceSchedulePlan.query.one()
        admin_day = next(day for day in admin_plan.days if day.work_date == period_start.isoformat())
        self.assertEqual(admin_plan.status, "ADMIN_APPROVED")
        self.assertEqual(admin_day.start_time, "09:00")

        super_data = dict(data)
        super_data[f"start_time_{past_key}"] = "10:00"
        with patch("portal.routes._attendance_schedule_today", return_value=today):
            allowed_for_super_admin = self._post(self.super_admin, super_data)
        self.assertEqual(allowed_for_super_admin.status_code, 302)
        super_admin_plan = HRAttendanceSchedulePlan.query.order_by(
            HRAttendanceSchedulePlan.version_no.desc(),
        ).first()
        super_admin_day = next(
            day for day in super_admin_plan.days if day.work_date == period_start.isoformat()
        )
        self.assertEqual(super_admin_plan.status, "ADMIN_APPROVED")
        self.assertEqual(super_admin_day.start_time, "10:00")

    def test_secretary_waits_for_assigned_general_director(self):
        general_director = User(
            name="\u0627\u0644\u0645\u062f\u064a\u0631 \u0627\u0644\u0639\u0627\u0645",
            email="weekly-general-director@example.test",
            password_hash="x",
            role="GENERAL_DIRECTOR",
        )
        db.session.add(general_director)
        db.session.commit()
        period_start = self._future_period_start()

        with patch("portal.routes.resolve_general_director", return_value=general_director):
            self._post(self.hr, {
                "target_user_id": str(self.employee.id),
                "period_start": period_start.isoformat(),
                "action": "hr_publish",
                **self._week_form(period_start),
            })
            baseline = HRAttendanceSchedulePlan.query.filter_by(
                user_id=self.employee.id,
            ).one()
            self._post(self.employee, {
                "target_user_id": str(self.employee.id),
                "period_start": period_start.isoformat(),
                "plan_id": str(baseline.id),
                "action": "request_change",
                "employee_note": "\u0623\u0637\u0644\u0628 \u062a\u063a\u064a\u064a\u0631 \u0627\u0644\u062c\u062f\u0648\u0644.",
            })
            change = HRAttendanceSchedulePlan.query.order_by(
                HRAttendanceSchedulePlan.id.desc(),
            ).first()
            self._post(self.manager, {
                "target_user_id": str(self.employee.id),
                "period_start": period_start.isoformat(),
                "plan_id": str(change.id),
                "action": "manager_approve",
            })

            self._post(self.secretary, {
                "target_user_id": str(self.employee.id),
                "period_start": period_start.isoformat(),
                "plan_id": str(change.id),
                "action": "final_approve",
            })
            db.session.refresh(change)
            self.assertEqual(change.status, "MANAGER_APPROVED")

            self._post(general_director, {
                "target_user_id": str(self.employee.id),
                "period_start": period_start.isoformat(),
                "plan_id": str(change.id),
                "action": "general_director_approve",
            })
            db.session.refresh(change)
            self.assertEqual(change.status, "GENERAL_DIRECTOR_APPROVED")

            self._post(self.secretary, {
                "target_user_id": str(self.employee.id),
                "period_start": period_start.isoformat(),
                "plan_id": str(change.id),
                "action": "final_approve",
            })
            db.session.refresh(change)
            self.assertEqual(change.status, "FINAL_APPROVED")


if __name__ == "__main__":
    unittest.main()
