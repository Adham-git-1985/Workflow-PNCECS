import inspect
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from flask import Flask

from extensions import db
from models import (
    EmployeeFile,
    HRAttendanceScheduleDay,
    HRAttendanceSchedulePlan,
    SystemSetting,
    User,
    WorkSchedule,
)
from portal.routes import (
    _attendance_schedule_week_rows,
    _effective_schedule_for_user,
    hr_work_schedule_final_approve_all,
    hr_work_schedule_super_admin_publish_batch,
    hr_work_schedule_update,
)
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

    def _post_batch(self, actor, data):
        fn = inspect.unwrap(hr_work_schedule_super_admin_publish_batch)
        with self.app.test_request_context(
            "/portal/hr/attendance/work-schedule/super-admin-publish-batch",
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

    def _week_form_for_schedule(self, start, schedule, start_time, end_time):
        values = self._week_form(start)
        for offset in range(7):
            day = start + timedelta(days=offset)
            key = day.strftime("%Y_%m_%d")
            values[f"schedule_id_{key}"] = str(schedule.id)
            values[f"start_time_{key}"] = start_time
            values[f"end_time_{key}"] = end_time
        return values

    def _add_employee(self, suffix):
        employee = User(
            email=f"weekly-employee-{suffix}@example.test",
            name=f"موظف أسبوعي {suffix}",
            password_hash="x",
            role="EMPLOYEE",
        )
        db.session.add(employee)
        db.session.flush()
        db.session.add(EmployeeFile(
            user_id=employee.id,
            direct_manager_user_id=self.manager.id,
        ))
        db.session.commit()
        return employee

    def _future_period_start(self):
        return attendance_schedule_cycle_start(date.today() + timedelta(days=14))

    def test_date_selects_one_sunday_to_saturday_week(self):
        self.assertEqual(attendance_schedule_cycle_start(date(2026, 9, 10)), date(2026, 9, 6))
        self.assertEqual(attendance_schedule_cycle_start(date(2026, 9, 19)), date(2026, 9, 13))
        self.assertEqual(len(attendance_schedule_cycle_days(date(2026, 9, 6))), 7)

    def test_hr_publishes_baseline_and_employee_request_is_not_effective_before_final_approval(self):
        period_start = self._future_period_start()
        changed_day = period_start + timedelta(days=1)
        changed_day_key = changed_day.strftime("%Y_%m_%d")
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
        self.assertEqual(
            _effective_schedule_for_user(self.employee.id, changed_day.isoformat()).start_time,
            "08:00",
        )

        request = self._post(self.employee, {
            "target_user_id": str(self.employee.id),
            "period_start": period_start.isoformat(),
            "plan_id": str(baseline.id),
            "action": "request_change",
            **self._week_form(period_start),
            f"start_time_{changed_day_key}": "09:00",
            f"end_time_{changed_day_key}": "16:00",
            "employee_note": "أطلب تغيير دوامي يوم الاثنين إلى دوام عن بعد.",
        })
        self.assertEqual(request.status_code, 302)
        change = HRAttendanceSchedulePlan.query.order_by(HRAttendanceSchedulePlan.id.desc()).first()
        self.assertEqual(change.request_type, "CHANGE_REQUEST")
        self.assertEqual(change.status, "SUBMITTED")
        changed_request_day = next(
            day for day in change.days if day.work_date == changed_day.isoformat()
        )
        self.assertEqual(changed_request_day.start_time, "09:00")
        self.assertEqual(changed_request_day.end_time, "16:00")
        self.assertEqual(
            _effective_schedule_for_user(self.employee.id, changed_day.isoformat()).start_time,
            "08:00",
        )

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
        self.assertEqual(
            _effective_schedule_for_user(self.employee.id, changed_day.isoformat()).start_time,
            "09:00",
        )

    def test_hr_schedule_pattern_remains_effective_after_a_month(self):
        effective_start = date(2031, 9, 22)
        remote_schedule = WorkSchedule(
            name="دوام عن بعد مستمر",
            kind="REMOTE",
            start_time="08:00",
            end_time="15:00",
            is_active=True,
        )
        db.session.add(remote_schedule)
        db.session.commit()
        form = self._week_form_for_schedule(
            effective_start,
            remote_schedule,
            "08:00",
            "15:00",
        )
        for offset in range(7):
            day_key = (effective_start + timedelta(days=offset)).strftime("%Y_%m_%d")
            form[f"day_type_{day_key}"] = "REMOTE"

        response = self._post(self.hr, {
            "target_user_id": str(self.employee.id),
            "period_start": effective_start.isoformat(),
            "action": "hr_publish",
            **form,
        })

        self.assertEqual(response.status_code, 302)
        plan = HRAttendanceSchedulePlan.query.filter_by(user_id=self.employee.id).one()
        self.assertEqual(plan.period_start, "2031-09-22")
        after_one_month = date(2031, 10, 22)
        effective = _effective_schedule_for_user(self.employee.id, after_one_month.isoformat())
        self.assertEqual(effective.kind, "REMOTE")
        self.assertEqual(effective.start_time, "08:00")

        week_dates, rows = _attendance_schedule_week_rows(
            [self.employee],
            date(2031, 10, 19),
            1,
            {self.employee.id: plan},
        )
        self.assertEqual(len(week_dates), 7)
        self.assertTrue(all(day["day_type"] == "REMOTE" for day in rows[0]["days"]))

    def test_approved_employee_change_stays_effective_until_a_later_change(self):
        remote_start = date(2031, 9, 22)
        change_start = date(2031, 10, 1)
        remote_schedule = WorkSchedule(
            name="دوام عن بعد قبل التغيير",
            kind="REMOTE",
            start_time="08:00",
            end_time="15:00",
            is_active=True,
        )
        db.session.add(remote_schedule)
        db.session.commit()

        remote_form = self._week_form_for_schedule(
            remote_start,
            remote_schedule,
            "08:00",
            "15:00",
        )
        for offset in range(7):
            day_key = (remote_start + timedelta(days=offset)).strftime("%Y_%m_%d")
            remote_form[f"day_type_{day_key}"] = "REMOTE"
        self._post(self.hr, {
            "target_user_id": str(self.employee.id),
            "period_start": remote_start.isoformat(),
            "action": "hr_publish",
            **remote_form,
        })

        request = self._post(self.employee, {
            "target_user_id": str(self.employee.id),
            "period_start": change_start.isoformat(),
            "action": "request_change",
            "employee_note": "طلب التحول إلى دوام مكتبي.",
            **self._week_form(change_start),
        })
        self.assertEqual(request.status_code, 302)
        change = HRAttendanceSchedulePlan.query.order_by(
            HRAttendanceSchedulePlan.id.desc(),
        ).first()
        self.assertEqual(change.period_start, "2031-10-01")
        self.assertEqual(change.status, "SUBMITTED")

        december_day = date(2031, 12, 31)
        self.assertEqual(
            _effective_schedule_for_user(self.employee.id, december_day.isoformat()).kind,
            "REMOTE",
        )

        self._post(self.manager, {
            "target_user_id": str(self.employee.id),
            "period_start": change_start.isoformat(),
            "plan_id": str(change.id),
            "action": "manager_approve",
        })
        self._post(self.secretary, {
            "target_user_id": str(self.employee.id),
            "period_start": change_start.isoformat(),
            "plan_id": str(change.id),
            "action": "final_approve",
        })

        db.session.refresh(change)
        self.assertEqual(change.status, "FINAL_APPROVED")
        effective = _effective_schedule_for_user(self.employee.id, december_day.isoformat())
        self.assertEqual(effective.kind, "FIXED")
        self.assertEqual(effective.start_time, "08:00")

    def test_super_admin_publishes_an_individual_schedule_directly(self):
        period_start = self._future_period_start()
        changed_day = period_start + timedelta(days=1)
        changed_day_key = changed_day.strftime("%Y_%m_%d")

        response = self._post(self.super_admin, {
            "target_user_id": str(self.employee.id),
            "period_start": period_start.isoformat(),
            "action": "super_publish",
            **self._week_form(period_start),
            f"start_time_{changed_day_key}": "09:00",
            f"end_time_{changed_day_key}": "16:00",
        })

        self.assertEqual(response.status_code, 302)
        plan = HRAttendanceSchedulePlan.query.filter_by(user_id=self.employee.id).one()
        self.assertEqual(plan.status, "FINAL_APPROVED")
        self.assertEqual(plan.final_approved_by_id, self.super_admin.id)
        self.assertEqual(
            _effective_schedule_for_user(self.employee.id, changed_day.isoformat()).start_time,
            "09:00",
        )

    def test_super_admin_can_final_approve_a_change_without_general_director_approval(self):
        general_director = User(
            email="weekly-super-general-director@example.test",
            name="مدير عام",
            password_hash="x",
            role="GENERAL_DIRECTOR",
        )
        db.session.add(general_director)
        db.session.commit()
        period_start = self._future_period_start()
        changed_day_key = (period_start + timedelta(days=1)).strftime("%Y_%m_%d")

        self._post(self.hr, {
            "target_user_id": str(self.employee.id),
            "period_start": period_start.isoformat(),
            "action": "hr_publish",
            **self._week_form(period_start),
        })
        baseline = HRAttendanceSchedulePlan.query.filter_by(
            user_id=self.employee.id,
            request_type="BASELINE",
        ).one()
        self._post(self.employee, {
            "target_user_id": str(self.employee.id),
            "period_start": period_start.isoformat(),
            "plan_id": str(baseline.id),
            "action": "request_change",
            "employee_note": "أطلب تغيير وقت الدوام.",
            **self._week_form(period_start),
            f"start_time_{changed_day_key}": "09:00",
            f"end_time_{changed_day_key}": "16:00",
        })
        change = HRAttendanceSchedulePlan.query.order_by(
            HRAttendanceSchedulePlan.id.desc(),
        ).first()
        change.general_director_user_id = general_director.id
        db.session.commit()

        response = self._post(self.super_admin, {
            "target_user_id": str(self.employee.id),
            "period_start": period_start.isoformat(),
            "plan_id": str(change.id),
            "action": "final_approve",
        })

        self.assertEqual(response.status_code, 302)
        db.session.refresh(change)
        self.assertEqual(change.status, "FINAL_APPROVED")
        self.assertEqual(change.final_approved_by_id, self.super_admin.id)

    def test_super_admin_bulk_final_approval_bypasses_general_director(self):
        period_start = self._future_period_start()
        plan = HRAttendanceSchedulePlan(
            user_id=self.employee.id,
            manager_user_id=self.manager.id,
            general_director_user_id=self.manager.id,
            period_start=period_start.isoformat(),
            period_end=(period_start + timedelta(days=6)).isoformat(),
            version_no=1,
            status="SUBMITTED",
            request_type="CHANGE_REQUEST",
        )
        db.session.add(plan)
        db.session.flush()
        db.session.add_all(
            HRAttendanceScheduleDay(
                plan_id=plan.id,
                work_date=(period_start + timedelta(days=offset)).isoformat(),
                day_type="WORK",
                schedule_id=self.schedule.id,
                start_time="08:00",
                end_time="15:00",
            )
            for offset in range(7)
        )
        db.session.commit()

        fn = inspect.unwrap(hr_work_schedule_final_approve_all)
        with self.app.test_request_context(
            "/portal/hr/attendance/work-schedule/final-approve-all",
            method="POST",
            data={"period_start": period_start.isoformat()},
        ):
            with patch("portal.routes.current_user", self.super_admin), patch(
                "portal.routes.url_for",
                return_value="/portal/hr/attendance/work-schedule",
            ), patch("portal.routes.notify_attendance_schedule_stakeholders"), patch(
                "portal.routes._portal_audit"
            ):
                response = fn()

        self.assertEqual(response.status_code, 302)
        db.session.refresh(plan)
        self.assertEqual(plan.status, "FINAL_APPROVED")
        self.assertEqual(plan.final_approved_by_id, self.super_admin.id)

    def test_super_admin_batch_publish_only_changes_selected_employees(self):
        selected_employee = self._add_employee("selected")
        untouched_employee = self._add_employee("untouched")
        untouched_schedule = WorkSchedule(
            name="دوام الموظف غير المختار",
            kind="FIXED",
            start_time="07:00",
            end_time="14:00",
            is_active=True,
        )
        db.session.add(untouched_schedule)
        db.session.commit()
        period_start = self._future_period_start()
        observed_day = period_start + timedelta(days=1)

        self._post(self.hr, {
            "target_user_id": str(untouched_employee.id),
            "period_start": period_start.isoformat(),
            "action": "hr_publish",
            **self._week_form_for_schedule(
                period_start,
                untouched_schedule,
                "07:00",
                "14:00",
            ),
        })
        untouched_plan = HRAttendanceSchedulePlan.query.filter_by(
            user_id=untouched_employee.id,
        ).one()

        response = self._post_batch(self.super_admin, {
            "period_start": period_start.isoformat(),
            "target_user_ids": [str(self.employee.id), str(selected_employee.id)],
            **self._week_form_for_schedule(period_start, self.schedule, "10:00", "17:00"),
        })

        self.assertEqual(response.status_code, 302)
        published_plans = HRAttendanceSchedulePlan.query.filter(
            HRAttendanceSchedulePlan.user_id.in_((self.employee.id, selected_employee.id)),
        ).all()
        self.assertEqual(len(published_plans), 2)
        self.assertTrue(all(plan.status == "FINAL_APPROVED" for plan in published_plans))
        self.assertTrue(
            all(plan.final_approved_by_id == self.super_admin.id for plan in published_plans)
        )
        self.assertEqual(
            _effective_schedule_for_user(self.employee.id, observed_day.isoformat()).start_time,
            "10:00",
        )
        self.assertEqual(
            _effective_schedule_for_user(selected_employee.id, observed_day.isoformat()).start_time,
            "10:00",
        )
        self.assertEqual(
            HRAttendanceSchedulePlan.query.filter_by(user_id=untouched_employee.id).count(),
            1,
        )
        db.session.refresh(untouched_plan)
        self.assertEqual(untouched_plan.status, "ADMIN_APPROVED")
        self.assertEqual(
            _effective_schedule_for_user(untouched_employee.id, observed_day.isoformat()).start_time,
            "07:00",
        )

    def test_employee_can_cancel_an_open_change_request_and_submit_a_new_one(self):
        period_start = self._future_period_start()
        changed_day = period_start + timedelta(days=1)
        changed_day_key = changed_day.strftime("%Y_%m_%d")
        self._post(self.hr, {
            "target_user_id": str(self.employee.id),
            "period_start": period_start.isoformat(),
            "action": "hr_publish",
            **self._week_form(period_start),
        })
        baseline = HRAttendanceSchedulePlan.query.filter_by(
            user_id=self.employee.id,
            request_type="BASELINE",
        ).one()

        self._post(self.employee, {
            "target_user_id": str(self.employee.id),
            "period_start": period_start.isoformat(),
            "plan_id": str(baseline.id),
            "action": "request_change",
            "employee_note": "أطلب تعديل وقت الدوام.",
            **self._week_form(period_start),
            f"start_time_{changed_day_key}": "09:00",
            f"end_time_{changed_day_key}": "16:00",
        })
        request_plan = HRAttendanceSchedulePlan.query.order_by(
            HRAttendanceSchedulePlan.id.desc(),
        ).first()
        self.assertEqual(request_plan.status, "SUBMITTED")

        cancelled = self._post(self.employee, {
            "target_user_id": str(self.employee.id),
            "period_start": period_start.isoformat(),
            "plan_id": str(request_plan.id),
            "action": "cancel_change",
        })
        self.assertEqual(cancelled.status_code, 302)
        db.session.refresh(request_plan)
        self.assertEqual(request_plan.status, "CANCELLED")
        self.assertEqual(
            _effective_schedule_for_user(self.employee.id, changed_day.isoformat()).start_time,
            "08:00",
        )

        self._post(self.employee, {
            "target_user_id": str(self.employee.id),
            "period_start": period_start.isoformat(),
            "plan_id": str(request_plan.id),
            "action": "request_change",
            "employee_note": "أطلب تعديلًا جديدًا لوقت الدوام.",
            **self._week_form(period_start),
            f"start_time_{changed_day_key}": "10:00",
            f"end_time_{changed_day_key}": "17:00",
        })
        replacement = HRAttendanceSchedulePlan.query.order_by(
            HRAttendanceSchedulePlan.id.desc(),
        ).first()
        self.assertNotEqual(replacement.id, request_plan.id)
        self.assertEqual(replacement.status, "SUBMITTED")

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

        employee_data = {
            "target_user_id": str(self.employee.id),
            "period_start": period_start.isoformat(),
            "action": "request_change",
            "employee_note": "أطلب تعديل يوم سابق.",
            **self._week_form(period_start),
        }
        with patch("portal.routes._attendance_schedule_today", return_value=today):
            employee_blocked = self._post(self.employee, employee_data)
        self.assertEqual(employee_blocked.status_code, 302)
        self.assertEqual(HRAttendanceSchedulePlan.query.count(), 0)

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
        self.assertEqual(super_admin_plan.status, "FINAL_APPROVED")
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
                **self._week_form(period_start),
                f"start_time_{(period_start + timedelta(days=1)).strftime('%Y_%m_%d')}": "09:00",
                f"end_time_{(period_start + timedelta(days=1)).strftime('%Y_%m_%d')}": "16:00",
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
