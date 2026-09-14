import unittest
from datetime import date, datetime
from pathlib import Path

from flask import Flask
from flask_login import LoginManager, login_user, logout_user

from extensions import db
from models import (
    AttendanceDailySummary,
    AttendanceEvent,
    EmployeeFile,
    HRAttendanceScheduleDay,
    HRAttendanceSchedulePlan,
    HRAttendanceSpecialCase,
    HRLeaveRequest,
    HRLeaveType,
    HRLookupItem,
    HRRequestApprovalStep,
    Notification,
    OrgNode,
    OrgNodeManager,
    OrgNodeType,
    SystemSetting,
    User,
    UserPermission,
    WorkSchedule,
)
from portal import portal_bp
from portal.routes import (
    ATTENDANCE_AUTO_LEAVE_SOURCE,
    _administrative_affairs_daily_rows,
    _attach_manual_attendance_flags,
    _casual_leave_policy_error,
    _convert_auto_annual_leave_after_sick_approval,
    _leave_used_days,
    _manual_attendance_event_rows_for_report,
    _process_unrecorded_office_attendance,
    hr_attendance_manual_edit,
    hr_attendance_manual_review,
)
from services.hr_request_workflow import (
    KIND_LEAVE,
    STAGE_DIRECT_MANAGER,
    STAGE_HR,
    STAGE_SECRETARY_GENERAL,
    administrative_affairs_manager_user_ids,
    hr_notification_user_ids,
    start_request_flow,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class AdministrativeAffairsAttendanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(
            __name__,
            template_folder=str(PROJECT_ROOT / "templates"),
        )
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="administrative-affairs-attendance-test",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(cls.app)
        LoginManager(cls.app)
        cls.app.register_blueprint(portal_bp)
        cls.context = cls.app.app_context()
        cls.context.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.context.pop()

    def setUp(self):
        db.session.remove()
        db.drop_all()
        db.create_all()
        # Friday/Saturday are the standard weekly holidays.
        db.session.add(SystemSetting(key="HR_WEEKLY_HOLIDAYS_MASK", value="48"))
        db.session.commit()

    def _user(self, email, name, role="EMPLOYEE", *, employee_file=True, **file_values):
        user = User(email=email, name=name, password_hash="x", role=role)
        db.session.add(user)
        db.session.flush()
        if employee_file:
            db.session.add(EmployeeFile(user_id=user.id, full_name_quad=name, **file_values))
        db.session.flush()
        return user

    def _final_schedule_day(self, user, work_day, day_type, *, manager=None, schedule=None):
        manager = manager or user
        plan = HRAttendanceSchedulePlan(
            user_id=user.id,
            manager_user_id=manager.id,
            period_start=work_day,
            period_end=work_day,
            version_no=1,
            status="FINAL_APPROVED",
        )
        db.session.add(plan)
        db.session.flush()
        day_row = HRAttendanceScheduleDay(
            plan_id=plan.id,
            work_date=work_day,
            day_type=day_type,
            schedule_id=schedule.id if schedule else None,
            start_time="08:00" if day_type == "WORK" else None,
            end_time="15:00" if day_type == "WORK" else None,
        )
        db.session.add(day_row)
        db.session.flush()
        return day_row

    def test_assigned_administrative_affairs_manager_is_resolved_without_hr_role(self):
        manager = self._user(
            "admin-affairs-manager@example.test",
            "مدير الشؤون الإدارية",
            "directorate_head",
            employee_file=False,
        )
        node_type = OrgNodeType(code="DIRECTORATE", name_ar="إدارة عامة")
        db.session.add(node_type)
        db.session.flush()
        node = OrgNode(
            type_id=node_type.id,
            name_ar="الإدارة العامة للشؤون الإدارية والمالية",
        )
        db.session.add(node)
        db.session.flush()
        db.session.add(OrgNodeManager(node_id=node.id, manager_user_id=manager.id))
        db.session.commit()

        self.assertIn(manager.id, administrative_affairs_manager_user_ids())
        self.assertIn(manager.id, hr_notification_user_ids())

    def test_unified_report_combines_present_leave_and_remote_employees(self):
        present = self._user("present@example.test", "موظف حاضر")
        on_leave = self._user("leave@example.test", "موظف مجاز")
        remote = self._user("remote@example.test", "موظف مناوب")
        annual = HRLeaveType(
            code="ANNUAL",
            name_ar="إجازة سنوية",
            day_count_basis="CALENDAR_DAYS",
        )
        db.session.add(annual)
        db.session.flush()
        db.session.add_all((
            AttendanceEvent(
                user_id=present.id,
                event_dt=datetime(2026, 9, 14, 8, 1),
                event_type="I",
            ),
            HRLeaveRequest(
                user_id=on_leave.id,
                leave_type_id=annual.id,
                start_date="2026-09-14",
                end_date="2026-09-14",
                days=1,
                entered_by="SELF",
                status="APPROVED",
            ),
        ))
        self._final_schedule_day(remote, "2026-09-14", "REMOTE")
        db.session.commit()

        rows = _administrative_affairs_daily_rows(
            date(2026, 9, 14),
            date(2026, 9, 14),
        )
        categories = {row["user_id"]: row["category"] for row in rows}

        self.assertEqual(categories[present.id], "PRESENT")
        self.assertEqual(categories[on_leave.id], "LEAVE")
        self.assertEqual(categories[remote.id], "REMOTE")

    def test_office_day_without_punch_creates_one_auto_annual_leave_and_notifications(self):
        employee = self._user("missing@example.test", "موظف بلا بصمة")
        hr_user = self._user("hr@example.test", "الشؤون الإدارية", "HR", employee_file=False)
        secretary = self._user(
            "secretary@example.test",
            "الأمين العام",
            "GENERAL_SECRETARY",
            employee_file=False,
        )
        annual = HRLeaveType(
            code="ANNUAL",
            name_ar="إجازة سنوية",
            default_balance_days=30,
            deduct_from_balance=True,
            day_count_basis="CALENDAR_DAYS",
        )
        schedule = WorkSchedule(
            name="دوام مكتبي",
            kind="FIXED",
            start_time="08:00",
            end_time="15:00",
        )
        db.session.add_all((annual, schedule))
        db.session.flush()
        self._final_schedule_day(employee, "2026-09-14", "WORK", manager=hr_user, schedule=schedule)
        db.session.commit()

        first = _process_unrecorded_office_attendance(
            reference_dt=datetime(2026, 9, 14, 17, 0),
            day_from=date(2026, 9, 14),
            day_to=date(2026, 9, 14),
        )
        db.session.commit()
        second = _process_unrecorded_office_attendance(
            reference_dt=datetime(2026, 9, 14, 18, 0),
            day_from=date(2026, 9, 14),
            day_to=date(2026, 9, 14),
        )
        db.session.commit()

        auto_leave = HRLeaveRequest.query.filter_by(
            user_id=employee.id,
            source=ATTENDANCE_AUTO_LEAVE_SOURCE,
            source_attendance_day="2026-09-14",
        ).one()
        notified_ids = {
            row.user_id
            for row in Notification.query.filter_by(
                event_key=f"att-auto-leave-2026-09-14-{employee.id}"
            ).all()
        }
        self.assertEqual(first["created"], 1)
        self.assertEqual(second["created"], 0)
        self.assertEqual(auto_leave.status, "APPROVED")
        self.assertEqual(auto_leave.leave_type_id, annual.id)
        self.assertEqual(notified_ids, {employee.id, hr_user.id, secretary.id})

        # A delayed clock sync must restore attendance and release only the
        # machine-created annual charge for that day.
        db.session.add(AttendanceEvent(
            user_id=employee.id,
            event_dt=datetime(2026, 9, 14, 8, 3),
            event_type="I",
        ))
        db.session.commit()
        third = _process_unrecorded_office_attendance(
            reference_dt=datetime(2026, 9, 14, 19, 0),
            day_from=date(2026, 9, 14),
            day_to=date(2026, 9, 14),
        )
        db.session.commit()
        db.session.refresh(auto_leave)

        self.assertEqual(third["reversed"], 1)
        self.assertEqual(auto_leave.status, "CANCELLED")
        self.assertEqual(auto_leave.replacement_reason, "LATE_CLOCK_ATTENDANCE")
        self.assertEqual(_leave_used_days(employee.id, annual.id, 2026), 0.0)

    def test_final_sick_approval_releases_the_auto_annual_charge(self):
        employee = self._user("sick@example.test", "موظف مريض")
        annual = HRLeaveType(
            code="ANNUAL",
            name_ar="إجازة سنوية",
            default_balance_days=30,
            deduct_from_balance=True,
            day_count_basis="CALENDAR_DAYS",
        )
        sick_type = HRLeaveType(
            code="SICK",
            name_ar="إجازة مرضية",
            requires_documents=True,
            deduct_from_balance=False,
            day_count_basis="CALENDAR_DAYS",
        )
        db.session.add_all((annual, sick_type))
        db.session.flush()
        automatic = HRLeaveRequest(
            user_id=employee.id,
            leave_type_id=annual.id,
            start_date="2026-09-14",
            end_date="2026-09-14",
            days=1,
            entered_by="SYSTEM",
            source=ATTENDANCE_AUTO_LEAVE_SOURCE,
            source_attendance_day="2026-09-14",
            status="APPROVED",
        )
        sick_request = HRLeaveRequest(
            user_id=employee.id,
            leave_type_id=sick_type.id,
            start_date="2026-09-14",
            end_date="2026-09-14",
            days=1,
            entered_by="SELF",
            status="APPROVED",
        )
        db.session.add_all((automatic, sick_request))
        db.session.commit()

        converted = _convert_auto_annual_leave_after_sick_approval(sick_request)
        db.session.commit()
        db.session.refresh(automatic)

        self.assertEqual(converted, 1)
        self.assertEqual(automatic.status, "CANCELLED")
        self.assertEqual(automatic.replaced_by_leave_request_id, sick_request.id)
        self.assertEqual(automatic.replacement_reason, "APPROVED_SICK_LEAVE")
        self.assertEqual(_leave_used_days(employee.id, annual.id, 2026), 0.0)

    def test_sick_leave_flow_includes_manager_hr_and_secretary_general(self):
        manager = self._user("manager@example.test", "المدير المباشر", "MANAGER", employee_file=False)
        employee = self._user(
            "workflow-sick@example.test",
            "موظف مرضي",
            direct_manager_user_id=manager.id,
        )
        self._user("workflow-hr@example.test", "مدير الشؤون الإدارية", "HR", employee_file=False)
        self._user(
            "workflow-secretary@example.test",
            "الأمين العام",
            "GENERAL_SECRETARY",
            employee_file=False,
        )
        sick_type = HRLeaveType(
            code="SICK",
            name_ar="إجازة مرضية",
            requires_documents=True,
            deduct_from_balance=False,
        )
        db.session.add(sick_type)
        db.session.flush()
        request_row = HRLeaveRequest(
            user_id=employee.id,
            leave_type_id=sick_type.id,
            start_date="2026-09-14",
            end_date="2026-09-14",
            days=1,
            status="SUBMITTED",
        )
        db.session.add(request_row)
        db.session.flush()

        steps = start_request_flow(KIND_LEAVE, request_row)
        db.session.commit()

        self.assertEqual(
            [step.stage_code for step in steps],
            [STAGE_DIRECT_MANAGER, STAGE_HR, STAGE_SECRETARY_GENERAL],
        )
        self.assertEqual(HRRequestApprovalStep.query.filter_by(request_id=request_row.id).count(), 3)

    def test_casual_leave_requires_confirmed_staff_and_zero_annual_balance(self):
        confirmed_type = HRLookupItem(
            category="APPOINTMENT_TYPE",
            code="PERMANENT",
            name_ar="مثبت",
            name_en="Permanent",
        )
        contract_type = HRLookupItem(
            category="APPOINTMENT_TYPE",
            code="CONTRACT",
            name_ar="عقد",
            name_en="Contract",
        )
        annual = HRLeaveType(
            code="ANNUAL",
            name_ar="إجازة سنوية",
            default_balance_days=2,
            deduct_from_balance=True,
            day_count_basis="CALENDAR_DAYS",
        )
        casual = HRLeaveType(
            code="CASUAL",
            name_ar="إجازة عارضة",
            deduct_from_balance=False,
            day_count_basis="CALENDAR_DAYS",
        )
        db.session.add_all((confirmed_type, contract_type, annual, casual))
        db.session.flush()
        confirmed = self._user(
            "confirmed@example.test",
            "موظف مثبت",
            appointment_type_lookup_id=confirmed_type.id,
        )
        contractor = self._user(
            "contractor@example.test",
            "موظف عقد",
            appointment_type_lookup_id=contract_type.id,
        )
        db.session.commit()

        remaining_error = _casual_leave_policy_error(
            confirmed.id,
            casual,
            date(2026, 9, 14),
            date(2026, 9, 14),
        )
        contract_error = _casual_leave_policy_error(
            contractor.id,
            casual,
            date(2026, 9, 14),
            date(2026, 9, 14),
        )
        db.session.add(HRLeaveRequest(
            user_id=confirmed.id,
            leave_type_id=annual.id,
            start_date="2026-01-05",
            end_date="2026-01-06",
            days=2,
            status="APPROVED",
        ))
        db.session.commit()
        exhausted_error = _casual_leave_policy_error(
            confirmed.id,
            casual,
            date(2026, 9, 14),
            date(2026, 9, 14),
        )

        self.assertIn("الرصيد المتبقي", remaining_error)
        self.assertIn("المثبتين فقط", contract_error)
        self.assertIsNone(exhausted_error)

    def test_start_only_manual_entry_needs_both_approvals_and_is_marked_manual(self):
        employee = self._user("manual-employee@example.test", "موظف يدوي")
        editor = self._user("manual-editor@example.test", "مدخل الدوام", "HR", employee_file=False)
        hr_approver = self._user("manual-hr@example.test", "مدير الشؤون الإدارية", "HR", employee_file=False)
        secretary = self._user(
            "manual-secretary@example.test",
            "الأمين العام",
            "GENERAL_SECRETARY",
            employee_file=False,
        )
        db.session.add_all((
            UserPermission(user_id=editor.id, key="HR_ATTENDANCE_EDIT", is_allowed=True),
            UserPermission(user_id=hr_approver.id, key="HR_ATTENDANCE_EDIT_APPROVE", is_allowed=True),
            UserPermission(user_id=secretary.id, key="HR_ATTENDANCE_EDIT_APPROVE", is_allowed=True),
        ))
        db.session.commit()

        with self.app.test_request_context(
            "/portal/hr/attendance/manual",
            method="POST",
            data={
                "user_id": str(employee.id),
                "day": "2026-09-14",
                "day_to": "2026-09-14",
                "start_time": "08:05",
                "end_time": "",
                "note": "تأكيد حضور هاتفي",
            },
        ):
            login_user(editor)
            response = hr_attendance_manual_edit()
            self.assertEqual(response.status_code, 302)
            logout_user()

        correction = HRAttendanceSpecialCase.query.one()
        with self.app.test_request_context(
            f"/portal/hr/attendance/manual/{correction.id}/review",
            method="POST",
            data={"action": "approve", "approval_note": "اعتماد الشؤون الإدارية"},
        ):
            login_user(hr_approver)
            response = hr_attendance_manual_review(correction.id)
            self.assertEqual(response.status_code, 302)
            logout_user()

        db.session.refresh(correction)
        self.assertEqual(correction.approval_status, "PENDING")
        self.assertFalse(correction.applied)
        self.assertIsNone(
            AttendanceDailySummary.query.filter_by(user_id=employee.id, day="2026-09-14").first()
        )

        with self.app.test_request_context(
            f"/portal/hr/attendance/manual/{correction.id}/review",
            method="POST",
            data={"action": "approve", "approval_note": "اعتماد نهائي"},
        ):
            login_user(secretary)
            response = hr_attendance_manual_review(correction.id)
            self.assertEqual(response.status_code, 302)
            logout_user()

        db.session.refresh(correction)
        summary = AttendanceDailySummary.query.filter_by(
            user_id=employee.id,
            day="2026-09-14",
        ).one()
        _attach_manual_attendance_flags([summary])
        manual_events = _manual_attendance_event_rows_for_report(
            date(2026, 9, 14),
            date(2026, 9, 14),
            user_id=employee.id,
        )

        self.assertEqual(correction.approval_status, "APPROVED")
        self.assertTrue(correction.applied)
        self.assertEqual(correction.approved_by_id, hr_approver.id)
        self.assertEqual(correction.final_approved_by_id, secretary.id)
        self.assertEqual(summary.first_in.strftime("%H:%M"), "08:05")
        self.assertIsNone(summary.last_out)
        self.assertEqual(summary.status, "INCOMPLETE")
        self.assertTrue(summary.manual_attendance)
        self.assertEqual(len(manual_events), 1)
        self.assertEqual(manual_events[0].event_type, "I")
        self.assertEqual(manual_events[0].display_event_label, "دخول يدوي")
        self.assertTrue(manual_events[0].is_manual_attendance)

    def test_changed_templates_compile(self):
        for template_name in (
            "portal/hr/reports_administrative_affairs_daily.html",
            "portal/hr/attendance_daily.html",
            "portal/hr/print/attendance_daily.html",
            "portal/hr/attendance_events.html",
            "portal/hr/print/attendance_events.html",
            "portal/hr/attendance_manual_edit.html",
            "portal/hr/attendance_manual_approval_queue.html",
        ):
            with self.subTest(template=template_name):
                self.app.jinja_env.get_template(template_name)

if __name__ == "__main__":
    unittest.main()
