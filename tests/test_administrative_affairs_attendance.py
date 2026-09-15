import io
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from flask_login import LoginManager, login_user, logout_user
from werkzeug.exceptions import Forbidden

from extensions import db
from models import (
    AttendanceDailySummary,
    AttendanceEvent,
    EmployeeFile,
    HRAttendanceScheduleDay,
    HRAttendanceSchedulePlan,
    HRAttendanceSpecialCase,
    HRLeaveAttachment,
    HRLeaveRequest,
    HRLeaveType,
    HRLookupItem,
    HROfficialOccasion,
    HRRequestApprovalStep,
    Notification,
    OrgNode,
    OrgNodeManager,
    OrgNodeType,
    SystemSetting,
    User,
    UserPermission,
    WorkAssignment,
    WorkPolicy,
    WorkSchedule,
)
from portal import portal_bp
from portal.routes import (
    ATTENDANCE_AUTO_LEAVE_SOURCE,
    _administrative_affairs_daily_rows,
    _attach_manual_attendance_flags,
    _casual_leave_policy_error,
    _can_review_manual_attendance,
    _convert_auto_annual_leave_after_sick_approval,
    _leave_used_days,
    _manual_attendance_event_rows_for_report,
    _process_unrecorded_office_attendance,
    hr_approval_leave,
    hr_attendance_manual_edit,
    hr_attendance_manual_review,
    hr_leaves_admin_edit,
    hr_leave_request_edit,
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

    def test_remote_schedule_template_is_not_classified_as_missing_punch(self):
        employee = self._user("remote-template@example.test", "موظف قالب عن بعد")
        annual = HRLeaveType(
            code="ANNUAL",
            name_ar="إجازة سنوية",
            default_balance_days=30,
            deduct_from_balance=True,
            day_count_basis="CALENDAR_DAYS",
        )
        remote_schedule = WorkSchedule(
            name="قالب العمل عن بعد",
            kind="REMOTE",
            start_time="08:00",
            end_time="15:00",
        )
        db.session.add_all((annual, remote_schedule))
        db.session.flush()
        # Legacy/previously saved rows may still say WORK while pointing to a
        # remote template. The template must keep the day out of office-missing.
        self._final_schedule_day(
            employee,
            "2026-09-14",
            "WORK",
            schedule=remote_schedule,
        )
        db.session.commit()

        processed = _process_unrecorded_office_attendance(
            reference_dt=datetime(2026, 9, 14, 17, 0),
            day_from=date(2026, 9, 14),
            day_to=date(2026, 9, 14),
        )
        report_row = _administrative_affairs_daily_rows(
            date(2026, 9, 14),
            date(2026, 9, 14),
            user_ids=[employee.id],
        )[0]

        self.assertEqual(processed["created"], 0)
        self.assertEqual(report_row["category"], "REMOTE")
        self.assertNotEqual(report_row["category"], "MISSING_PUNCH")
        self.assertEqual(
            HRLeaveRequest.query.filter_by(
                user_id=employee.id,
                source=ATTENDANCE_AUTO_LEAVE_SOURCE,
            ).count(),
            0,
        )

    def test_remote_work_policy_is_respected_without_a_schedule_day(self):
        employee = self._user("remote-policy@example.test", "موظف سياسة عن بعد")
        schedule = WorkSchedule(
            name="قالب ساعات العمل",
            kind="FIXED",
            start_time="08:00",
            end_time="15:00",
        )
        policy = WorkPolicy(
            name="سياسة عن بعد",
            days_policy="FIXED",
            fixed_days_mask=1,
            location_policy="REMOTE",
            is_active=True,
        )
        db.session.add_all((schedule, policy))
        db.session.flush()
        db.session.add(WorkAssignment(
            name="تكليف عن بعد",
            schedule_id=schedule.id,
            policy_id=policy.id,
            target_type="USER",
            target_user_id=employee.id,
            start_date="2026-09-14",
            end_date="2026-09-14",
            is_active=True,
        ))
        db.session.commit()

        report_row = _administrative_affairs_daily_rows(
            date(2026, 9, 14),
            date(2026, 9, 14),
            user_ids=[employee.id],
        )[0]

        self.assertEqual(report_row["category"], "REMOTE")
        self.assertNotEqual(report_row["category"], "MISSING_PUNCH")

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

    def test_auto_annual_is_reversed_when_office_schedule_changes_to_remote_or_off(self):
        remote_employee = self._user(
            "reclassified-remote@example.test",
            "Reclassified remote employee",
        )
        off_employee = self._user(
            "reclassified-off@example.test",
            "Reclassified off employee",
        )
        annual = HRLeaveType(
            code="ANNUAL",
            name_ar="Annual",
            default_balance_days=30,
            deduct_from_balance=True,
            day_count_basis="CALENDAR_DAYS",
        )
        schedule = WorkSchedule(
            name="Office schedule",
            kind="FIXED",
            start_time="08:00",
            end_time="15:00",
        )
        db.session.add_all((annual, schedule))
        db.session.flush()
        remote_day = self._final_schedule_day(
            remote_employee,
            "2026-09-14",
            "WORK",
            schedule=schedule,
        )
        off_day = self._final_schedule_day(
            off_employee,
            "2026-09-14",
            "WORK",
            schedule=schedule,
        )
        db.session.commit()

        initial = _process_unrecorded_office_attendance(
            reference_dt=datetime(2026, 9, 14, 17, 0),
            day_from=date(2026, 9, 14),
            day_to=date(2026, 9, 14),
        )
        db.session.commit()
        self.assertEqual(initial["created"], 2)

        remote_day.day_type = "REMOTE"
        remote_day.start_time = None
        remote_day.end_time = None
        off_day.day_type = "OFF"
        off_day.start_time = None
        off_day.end_time = None
        db.session.commit()

        reconciled = _process_unrecorded_office_attendance(
            reference_dt=datetime(2026, 9, 14, 18, 0),
            day_from=date(2026, 9, 14),
            day_to=date(2026, 9, 14),
        )
        db.session.commit()
        auto_rows = (
            HRLeaveRequest.query
            .filter(HRLeaveRequest.source == ATTENDANCE_AUTO_LEAVE_SOURCE)
            .order_by(HRLeaveRequest.user_id.asc())
            .all()
        )
        categories = {
            row["user_id"]: row["category"]
            for row in _administrative_affairs_daily_rows(
                date(2026, 9, 14),
                date(2026, 9, 14),
                user_ids=[remote_employee.id, off_employee.id],
            )
        }

        self.assertEqual(reconciled["reversed"], 2)
        self.assertEqual(len(auto_rows), 2)
        self.assertTrue(all(row.status == "CANCELLED" for row in auto_rows))
        self.assertTrue(all(row.replaced_at is not None for row in auto_rows))
        self.assertEqual(categories[remote_employee.id], "REMOTE")
        self.assertEqual(categories[off_employee.id], "OFF")

    def test_official_holiday_takes_priority_over_work_schedule_in_report(self):
        employee = self._user(
            "holiday-work-schedule@example.test",
            "Scheduled employee on holiday",
        )
        annual = HRLeaveType(
            code="ANNUAL",
            name_ar="Annual",
            default_balance_days=30,
            deduct_from_balance=True,
            day_count_basis="CALENDAR_DAYS",
        )
        schedule = WorkSchedule(
            name="Office schedule",
            kind="FIXED",
            start_time="08:00",
            end_time="15:00",
        )
        db.session.add_all((
            annual,
            schedule,
            HROfficialOccasion(
                title="Official holiday",
                day="2026-09-14",
                is_day_off=True,
            ),
        ))
        db.session.flush()
        self._final_schedule_day(
            employee,
            "2026-09-14",
            "WORK",
            schedule=schedule,
        )
        db.session.commit()

        processed = _process_unrecorded_office_attendance(
            reference_dt=datetime(2026, 9, 14, 17, 0),
            day_from=date(2026, 9, 14),
            day_to=date(2026, 9, 14),
        )
        db.session.commit()
        report_row = _administrative_affairs_daily_rows(
            date(2026, 9, 14),
            date(2026, 9, 14),
            user_ids=[employee.id],
        )[0]

        self.assertEqual(processed["created"], 0)
        self.assertEqual(
            HRLeaveRequest.query.filter_by(
                user_id=employee.id,
                source=ATTENDANCE_AUTO_LEAVE_SOURCE,
            ).count(),
            0,
        )
        self.assertEqual(report_row["category"], "OFF")
        self.assertNotEqual(report_row["category"], "MISSING_PUNCH")

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
        db.session.flush()
        db.session.add(HRLeaveAttachment(
            request_id=sick_request.id,
            doc_type="REPORT",
            original_name="medical-report.pdf",
            stored_name="medical-report.pdf",
            uploaded_by_id=employee.id,
        ))
        db.session.commit()

        converted = _convert_auto_annual_leave_after_sick_approval(sick_request)
        db.session.commit()
        db.session.refresh(automatic)

        self.assertEqual(converted, 1)
        self.assertEqual(automatic.status, "CANCELLED")
        self.assertEqual(automatic.replaced_by_leave_request_id, sick_request.id)
        self.assertEqual(automatic.replacement_reason, "APPROVED_SICK_LEAVE")
        self.assertEqual(_leave_used_days(employee.id, annual.id, 2026), 0.0)

    def test_approved_sick_leave_without_supporting_document_does_not_release_auto_annual(self):
        employee = self._user("sick-no-document@example.test", "Sick without document")
        annual = HRLeaveType(
            code="ANNUAL",
            name_ar="Annual",
            default_balance_days=30,
            deduct_from_balance=True,
            day_count_basis="CALENDAR_DAYS",
        )
        sick_type = HRLeaveType(
            code="SICK",
            name_ar="Sick",
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

        self.assertEqual(converted, 0)
        self.assertEqual(automatic.status, "APPROVED")
        self.assertIsNone(automatic.replaced_by_leave_request_id)
        self.assertIsNone(automatic.replaced_at)

    def test_sick_leave_cannot_advance_approval_without_supporting_document(self):
        manager = self._user(
            "sick-document-manager@example.test",
            "Direct manager",
            "MANAGER",
            employee_file=False,
        )
        employee = self._user(
            "sick-document-employee@example.test",
            "Sick employee",
            direct_manager_user_id=manager.id,
        )
        self._user(
            "sick-document-hr@example.test",
            "Administrative affairs",
            "HR_MANAGER",
            employee_file=False,
        )
        self._user(
            "sick-document-secretary@example.test",
            "Secretary General",
            "GENERAL_SECRETARY",
            employee_file=False,
        )
        db.session.add(UserPermission(
            user_id=manager.id,
            key="PORTAL_READ",
            is_allowed=True,
        ))
        sick_type = HRLeaveType(
            code="SICK",
            name_ar="Sick",
            requires_documents=True,
            deduct_from_balance=False,
        )
        db.session.add(sick_type)
        db.session.flush()
        sick_request = HRLeaveRequest(
            user_id=employee.id,
            leave_type_id=sick_type.id,
            start_date="2026-09-14",
            end_date="2026-09-14",
            days=1,
            entered_by="SELF",
            status="SUBMITTED",
            submitted_at=datetime.utcnow(),
        )
        db.session.add(sick_request)
        db.session.flush()
        steps = start_request_flow(KIND_LEAVE, sick_request)
        db.session.commit()

        with self.app.test_request_context(
            f"/portal/hr/approvals/leaves/{sick_request.id}",
            method="POST",
            data={
                "action": "APPROVE",
                "covering_employee_name": "Covering employee",
                "decision_note": "Reviewed",
            },
        ):
            login_user(manager)
            response = hr_approval_leave(sick_request.id)
            self.assertEqual(response.status_code, 302)
            logout_user()

        db.session.refresh(sick_request)
        db.session.refresh(steps[0])
        self.assertEqual(sick_request.status, "SUBMITTED")
        self.assertEqual(steps[0].status, "PENDING")
        self.assertIsNone(steps[0].decided_at)

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

    def test_employee_edit_from_annual_to_sick_restarts_the_approval_route(self):
        manager = self._user(
            "edit-leave-manager@example.test",
            "Direct manager",
            "MANAGER",
            employee_file=False,
        )
        employee = self._user(
            "edit-leave-employee@example.test",
            "Employee editing leave",
            direct_manager_user_id=manager.id,
        )
        self._user(
            "edit-leave-hr@example.test",
            "Administrative affairs",
            "HR_MANAGER",
            employee_file=False,
        )
        self._user(
            "edit-leave-secretary@example.test",
            "Secretary General",
            "GENERAL_SECRETARY",
            employee_file=False,
        )
        annual = HRLeaveType(
            code="ANNUAL",
            name_ar="Annual",
            requires_approval=True,
            is_active=True,
        )
        sick = HRLeaveType(
            code="SICK",
            name_ar="Sick",
            requires_approval=True,
            requires_documents=True,
            is_active=True,
        )
        db.session.add_all((annual, sick))
        db.session.flush()
        db.session.add_all([
            UserPermission(user_id=employee.id, key=key, is_allowed=True)
            for key in ("PORTAL_READ", "HR_READ", "HR_REQUESTS_CREATE")
        ])
        request_row = HRLeaveRequest(
            user_id=employee.id,
            leave_type_id=annual.id,
            start_date="2026-09-14",
            end_date="2026-09-14",
            days=1,
            status="SUBMITTED",
            submitted_at=datetime.utcnow(),
        )
        db.session.add(request_row)
        db.session.flush()
        original_steps = start_request_flow(KIND_LEAVE, request_row)
        original_step_ids = {step.id for step in original_steps}
        db.session.commit()

        def save_medical_report(files, request_id, *, doc_type):
            self.assertTrue(files)
            db.session.add(HRLeaveAttachment(
                request_id=request_id,
                doc_type=doc_type,
                original_name="medical-report.pdf",
                stored_name="medical-report.pdf",
                uploaded_by_id=employee.id,
            ))
            db.session.flush()
            return 1

        with self.app.test_request_context(
            f"/portal/hr/me/leaves/{request_row.id}/edit",
            method="POST",
            data={
                "leave_type_id": str(sick.id),
                "start_date": "2026-09-14",
                "end_date": "2026-09-14",
                "note": "Reported sick later that day",
                "attachments": (io.BytesIO(b"medical evidence"), "medical-report.pdf"),
            },
            content_type="multipart/form-data",
        ), patch("portal.routes._save_leave_files", side_effect=save_medical_report):
            login_user(employee)
            response = hr_leave_request_edit(request_row.id)
            self.assertEqual(response.status_code, 302)
            logout_user()

        db.session.expire_all()
        updated = db.session.get(HRLeaveRequest, request_row.id)
        all_steps = (
            HRRequestApprovalStep.query
            .filter_by(request_kind=KIND_LEAVE, request_id=request_row.id)
            .order_by(HRRequestApprovalStep.step_order.asc())
            .all()
        )
        restarted_steps = [step for step in all_steps if step.id not in original_step_ids]

        self.assertEqual(updated.leave_type_id, sick.id)
        self.assertEqual(updated.status, "SUBMITTED")
        self.assertEqual([step.status for step in all_steps if step.id in original_step_ids], ["CANCELLED"])
        self.assertEqual(
            [step.stage_code for step in restarted_steps],
            [STAGE_DIRECT_MANAGER, STAGE_HR, STAGE_SECRETARY_GENERAL],
        )
        self.assertEqual({step.flow_revision for step in restarted_steps}, {2})
        self.assertEqual(restarted_steps[0].status, "PENDING")
        self.assertTrue(all(step.status == "WAITING" for step in restarted_steps[1:]))
        self.assertEqual(HRLeaveAttachment.query.filter_by(request_id=request_row.id).count(), 1)

    def test_admin_cannot_edit_an_approved_leave_request(self):
        admin = self._user(
            "admin-edit-approved-leave@example.test",
            "Administrative editor",
            "ADMIN",
            employee_file=False,
        )
        employee = self._user(
            "approved-leave-employee@example.test",
            "Employee with approved leave",
        )
        annual = HRLeaveType(
            code="ANNUAL",
            name_ar="Annual",
            requires_approval=True,
            is_active=True,
        )
        db.session.add(annual)
        db.session.flush()
        request_row = HRLeaveRequest(
            user_id=employee.id,
            leave_type_id=annual.id,
            start_date="2026-09-14",
            end_date="2026-09-14",
            days=1,
            note="Original approved request",
            status="APPROVED",
        )
        db.session.add(request_row)
        db.session.commit()

        with self.app.test_request_context(
            f"/portal/hr/leaves/admin/{request_row.id}/edit",
            method="POST",
            data={
                "leave_type_id": str(annual.id),
                "start_date": "2026-09-15",
                "end_date": "2026-09-15",
                "note": "Must not be saved",
            },
        ), patch("portal.routes.start_request_flow") as restart_flow:
            login_user(admin)
            response = hr_leaves_admin_edit(request_row.id)
            self.assertEqual(response.status_code, 302)
            logout_user()

        db.session.expire_all()
        unchanged = db.session.get(HRLeaveRequest, request_row.id)
        self.assertEqual(unchanged.status, "APPROVED")
        self.assertEqual(unchanged.start_date, "2026-09-14")
        self.assertEqual(unchanged.end_date, "2026-09-14")
        self.assertEqual(unchanged.note, "Original approved request")
        restart_flow.assert_not_called()

    def test_admin_edit_from_annual_to_sick_restarts_the_approval_route(self):
        admin = self._user(
            "admin-edit-submitted-leave@example.test",
            "Administrative editor",
            "ADMIN",
            employee_file=False,
        )
        manager = self._user(
            "admin-edit-leave-manager@example.test",
            "Direct manager",
            "MANAGER",
            employee_file=False,
        )
        employee = self._user(
            "admin-edit-leave-employee@example.test",
            "Employee whose leave is edited",
            direct_manager_user_id=manager.id,
        )
        self._user(
            "admin-edit-leave-hr@example.test",
            "Administrative affairs manager",
            "HR_MANAGER",
            employee_file=False,
        )
        self._user(
            "admin-edit-leave-secretary@example.test",
            "Secretary General",
            "GENERAL_SECRETARY",
            employee_file=False,
        )
        annual = HRLeaveType(
            code="ANNUAL",
            name_ar="Annual",
            requires_approval=True,
            is_active=True,
        )
        sick = HRLeaveType(
            code="SICK",
            name_ar="Sick",
            requires_approval=True,
            requires_documents=True,
            is_active=True,
        )
        db.session.add_all((annual, sick))
        db.session.flush()
        request_row = HRLeaveRequest(
            user_id=employee.id,
            leave_type_id=annual.id,
            start_date="2026-09-14",
            end_date="2026-09-14",
            days=1,
            note="Annual leave",
            status="SUBMITTED",
            submitted_at=datetime.utcnow(),
        )
        db.session.add(request_row)
        db.session.flush()
        original_steps = start_request_flow(KIND_LEAVE, request_row)
        original_step_ids = {step.id for step in original_steps}
        db.session.commit()

        def save_medical_report(files, request_id, *, doc_type):
            self.assertTrue(files)
            self.assertEqual(doc_type, "ADMIN_DOC")
            db.session.add(HRLeaveAttachment(
                request_id=request_id,
                doc_type=doc_type,
                original_name="medical-report.pdf",
                stored_name="medical-report.pdf",
                uploaded_by_id=admin.id,
            ))
            db.session.flush()
            return 1

        with self.app.test_request_context(
            f"/portal/hr/leaves/admin/{request_row.id}/edit",
            method="POST",
            data={
                "leave_type_id": str(sick.id),
                "start_date": "2026-09-14",
                "end_date": "2026-09-14",
                "note": "Reported sick later that day",
                "attachments": (io.BytesIO(b"medical evidence"), "medical-report.pdf"),
            },
            content_type="multipart/form-data",
        ), patch("portal.routes._save_leave_files", side_effect=save_medical_report):
            login_user(admin)
            response = hr_leaves_admin_edit(request_row.id)
            self.assertEqual(response.status_code, 302)
            logout_user()

        db.session.expire_all()
        updated = db.session.get(HRLeaveRequest, request_row.id)
        all_steps = (
            HRRequestApprovalStep.query
            .filter_by(request_kind=KIND_LEAVE, request_id=request_row.id)
            .order_by(HRRequestApprovalStep.step_order.asc())
            .all()
        )
        old_steps = [step for step in all_steps if step.id in original_step_ids]
        restarted_steps = [step for step in all_steps if step.id not in original_step_ids]

        self.assertEqual(updated.leave_type_id, sick.id)
        self.assertEqual(updated.status, "SUBMITTED")
        self.assertTrue(old_steps)
        self.assertTrue(all(step.status == "CANCELLED" for step in old_steps))
        self.assertTrue(all(step.decided_at is not None for step in old_steps))
        self.assertEqual(
            [step.stage_code for step in restarted_steps],
            [STAGE_DIRECT_MANAGER, STAGE_HR, STAGE_SECRETARY_GENERAL],
        )
        self.assertEqual({step.flow_revision for step in restarted_steps}, {2})
        self.assertEqual(restarted_steps[0].status, "PENDING")
        self.assertTrue(all(step.status == "WAITING" for step in restarted_steps[1:]))
        self.assertEqual(HRLeaveAttachment.query.filter_by(request_id=request_row.id).count(), 1)

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

    def test_casual_leave_accepts_tathbeet_classification_after_annual_is_exhausted(self):
        confirmed_type = HRLookupItem(
            category="APPOINTMENT_TYPE",
            code="CLASSIFIED_STAFF",
            name_ar="\u062a\u062b\u0628\u064a\u062a",
            name_en="",
        )
        annual = HRLeaveType(
            code="ANNUAL",
            name_ar="Annual",
            default_balance_days=1,
            deduct_from_balance=True,
            day_count_basis="CALENDAR_DAYS",
        )
        casual = HRLeaveType(
            code="CASUAL",
            name_ar="Casual",
            deduct_from_balance=False,
            day_count_basis="CALENDAR_DAYS",
        )
        db.session.add_all((confirmed_type, annual, casual))
        db.session.flush()
        employee = self._user(
            "tathbeet@example.test",
            "Tathbeet employee",
            appointment_type_lookup_id=confirmed_type.id,
        )
        db.session.add(HRLeaveRequest(
            user_id=employee.id,
            leave_type_id=annual.id,
            start_date="2026-01-05",
            end_date="2026-01-05",
            days=1,
            status="APPROVED",
        ))
        db.session.commit()

        policy_error = _casual_leave_policy_error(
            employee.id,
            casual,
            date(2026, 9, 14),
            date(2026, 9, 14),
        )

        self.assertIsNone(policy_error)

    def test_generic_attendance_approver_cannot_take_administrative_affairs_stage(self):
        employee = self._user("manual-scope-employee@example.test", "Manual scope employee")
        generic_approver = self._user(
            "manual-generic-approver@example.test",
            "Generic attendance approver",
            "EMPLOYEE",
            employee_file=False,
        )
        affairs_manager = self._user(
            "manual-affairs-manager@example.test",
            "Administrative affairs manager",
            "HR_MANAGER",
            employee_file=False,
        )
        self._user(
            "manual-scope-secretary@example.test",
            "Secretary General",
            "GENERAL_SECRETARY",
            employee_file=False,
        )
        db.session.add(UserPermission(
            user_id=generic_approver.id,
            key="HR_ATTENDANCE_EDIT_APPROVE",
            is_allowed=True,
        ))
        correction = HRAttendanceSpecialCase(
            user_id=employee.id,
            day="2026-09-14",
            day_to="2026-09-14",
            kind="MANUAL_ATTENDANCE",
            start_time="08:05",
            approval_status="PENDING",
            applied=False,
            created_by_id=generic_approver.id,
        )
        db.session.add(correction)
        db.session.commit()

        self.assertFalse(_can_review_manual_attendance(correction, generic_approver))
        self.assertTrue(_can_review_manual_attendance(correction, affairs_manager))

        with self.app.test_request_context(
            f"/portal/hr/attendance/manual/{correction.id}/review",
            method="POST",
            data={"action": "approve", "approval_note": "Generic approval"},
        ):
            login_user(generic_approver)
            with self.assertRaises(Forbidden):
                hr_attendance_manual_review(correction.id)
            logout_user()

        db.session.refresh(correction)
        self.assertEqual(correction.approval_status, "PENDING")
        self.assertFalse(correction.applied)
        self.assertIsNone(correction.approved_at)

    def test_manual_attendance_does_not_skip_secretary_stage_when_unconfigured(self):
        employee = self._user("manual-no-secretary-employee@example.test", "Manual employee")
        affairs_manager = self._user(
            "manual-no-secretary-manager@example.test",
            "Administrative affairs manager",
            "HR_MANAGER",
            employee_file=False,
        )
        correction = HRAttendanceSpecialCase(
            user_id=employee.id,
            day="2026-09-14",
            day_to="2026-09-14",
            kind="MANUAL_ATTENDANCE",
            start_time="08:05",
            approval_status="PENDING",
            applied=False,
            created_by_id=affairs_manager.id,
        )
        db.session.add(correction)
        db.session.commit()

        with self.app.test_request_context(
            f"/portal/hr/attendance/manual/{correction.id}/review",
            method="POST",
            data={"action": "approve", "approval_note": "First-stage approval"},
        ):
            login_user(affairs_manager)
            response = hr_attendance_manual_review(correction.id)
            self.assertEqual(response.status_code, 302)
            logout_user()

        db.session.refresh(correction)
        self.assertEqual(correction.approval_status, "PENDING")
        self.assertFalse(correction.applied)
        self.assertIsNone(correction.approved_by_id)
        self.assertIsNone(correction.approved_at)
        self.assertIsNone(correction.final_approved_by_id)
        self.assertIsNone(
            AttendanceDailySummary.query.filter_by(
                user_id=employee.id,
                day="2026-09-14",
            ).first()
        )

    def test_start_only_manual_entry_needs_both_approvals_and_is_marked_manual(self):
        employee = self._user("manual-employee@example.test", "موظف يدوي")
        editor = self._user("manual-editor@example.test", "مدخل الدوام", "HR", employee_file=False)
        hr_approver = self._user("manual-hr@example.test", "مدير الشؤون الإدارية", "HR_MANAGER", employee_file=False)
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
