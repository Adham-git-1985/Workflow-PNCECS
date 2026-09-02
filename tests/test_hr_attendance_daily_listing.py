import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask
from flask_login import LoginManager, login_user, logout_user

from extensions import db
from models import (
    AttendanceDailySummary,
    AttendanceEvent,
    EmployeeFile,
    HRAttendanceSpecialCase,
    HRLeaveRequest,
    HRLeaveType,
    HRRequestApprovalStep,
    User,
    UserPermission,
)
from portal import portal_bp
from portal.routes import (
    _attendance_count_day,
    _attendance_daily_without_absences,
    _attendance_absence_candidates,
    _attendance_event_date_range,
    _hr_can_approve_attendance_edit,
    _hr_can_edit_attendance,
    _maternity_leave_period_error,
    _summary_compute_one,
    _sort_and_number_attendance_daily_rows,
    hr_attendance_manual_edit,
    hr_attendance_manual_review,
    hr_leave_request_new,
    hr_maternity_departure_new,
    hr_maternity_departure_review,
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
        cls.app.register_blueprint(portal_bp)
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

    def test_manual_edit_and_approval_permissions_are_independent(self):
        hr_user = User(email="hr@example.test", name="HR", password_hash="x", role="HR")
        attendance_editor = User(
            email="secretary@example.test",
            name="Secretary General",
            password_hash="x",
            role="GENERAL-SECRETARY",
        )
        attendance_approver = User(
            email="approver@example.test",
            name="Attendance Approver",
            password_hash="x",
            role="HR",
        )
        super_admin = User(
            email="superadmin@example.test",
            name="Super Admin",
            password_hash="x",
            role="SUPER_ADMIN",
        )
        db.session.add_all((hr_user, attendance_editor, attendance_approver, super_admin))
        db.session.flush()
        db.session.add_all((
            UserPermission(
                user_id=attendance_editor.id,
                key="HR_ATTENDANCE_EDIT",
                is_allowed=True,
            ),
            UserPermission(
                user_id=attendance_approver.id,
                key="HR_ATTENDANCE_EDIT_APPROVE",
                is_allowed=True,
            ),
        ))
        db.session.commit()

        with self.app.test_request_context():
            login_user(hr_user)
            self.assertFalse(_hr_can_edit_attendance())
            self.assertFalse(_hr_can_approve_attendance_edit())
            logout_user()

            login_user(attendance_editor)
            self.assertTrue(_hr_can_edit_attendance())
            self.assertFalse(_hr_can_approve_attendance_edit())
            logout_user()

            login_user(attendance_approver)
            self.assertFalse(_hr_can_edit_attendance())
            self.assertTrue(_hr_can_approve_attendance_edit())
            logout_user()

            login_user(super_admin)
            self.assertTrue(_hr_can_edit_attendance())
            self.assertTrue(_hr_can_approve_attendance_edit())
            logout_user()

    def test_submitted_edit_affects_attendance_only_after_approval(self):
        employee = User(email="employee@example.test", name="Employee", password_hash="x", role="USER")
        editor = User(email="editor@example.test", name="Editor", password_hash="x", role="HR")
        approver = User(email="approver@example.test", name="Approver", password_hash="x", role="HR")
        db.session.add_all((employee, editor, approver))
        db.session.flush()
        db.session.add_all((
            UserPermission(user_id=editor.id, key="HR_ATTENDANCE_EDIT", is_allowed=True),
            UserPermission(user_id=approver.id, key="HR_ATTENDANCE_EDIT_APPROVE", is_allowed=True),
        ))
        db.session.commit()

        with self.app.test_request_context(
            "/portal/hr/attendance/manual",
            method="POST",
            data={
                "user_id": str(employee.id),
                "day": "2026-09-01",
                "day_to": "2026-09-01",
                "start_time": "08:00",
                "end_time": "15:00",
                "note": "تصحيح معتمد المصدر",
            },
        ):
            login_user(editor)
            response = hr_attendance_manual_edit()
            self.assertEqual(response.status_code, 302)
            logout_user()

        correction = HRAttendanceSpecialCase.query.one()
        self.assertEqual(correction.approval_status, "PENDING")
        self.assertFalse(correction.applied)
        self.assertIsNone(AttendanceDailySummary.query.filter_by(user_id=employee.id, day="2026-09-01").first())

        with self.app.test_request_context(
            f"/portal/hr/attendance/manual/{correction.id}/review",
            method="POST",
            data={"action": "approve", "approval_note": "تمت المراجعة"},
        ):
            login_user(approver)
            response = hr_attendance_manual_review(correction.id)
            self.assertEqual(response.status_code, 302)
            logout_user()

        correction = db.session.get(HRAttendanceSpecialCase, correction.id)
        summary = AttendanceDailySummary.query.filter_by(user_id=employee.id, day="2026-09-01").one()
        self.assertEqual(correction.approval_status, "APPROVED")
        self.assertTrue(correction.applied)
        self.assertEqual(correction.approved_by_id, approver.id)
        self.assertEqual(summary.first_in.hour, 8)
        self.assertEqual(summary.last_out.hour, 15)

    def _legacy_maternity_departure_workflow(self):
        employee = User(email="employee@example.test", name="Employee", password_hash="x", role="USER")
        editor = User(email="editor@example.test", name="Editor", password_hash="x", role="HR")
        approver = User(email="approver@example.test", name="Approver", password_hash="x", role="HR")
        db.session.add_all((employee, editor, approver))
        db.session.flush()
        db.session.add_all((
            UserPermission(user_id=editor.id, key="HR_ATTENDANCE_EDIT", is_allowed=True),
            UserPermission(user_id=approver.id, key="HR_ATTENDANCE_EDIT_APPROVE", is_allowed=True),
            AttendanceEvent(user_id=employee.id, event_dt=datetime(2026, 9, 1, 8, 0), event_type="IN"),
            AttendanceEvent(user_id=employee.id, event_dt=datetime(2026, 9, 1, 14, 0), event_type="OUT"),
        ))
        db.session.commit()

        schedule = SimpleNamespace(
            id=None,
            kind="FIXED",
            start_time="08:00",
            end_time="15:00",
            break_minutes=0,
            grace_minutes=0,
            start_grace_minutes=None,
            end_grace_minutes=None,
            overtime_threshold_minutes=0,
        )
        with patch("portal.routes._effective_schedule_for_user", return_value=schedule):
            self.assertEqual(_summary_compute_one(employee.id, "2026-09-01")["early_leave_minutes"], 60)

            with self.app.test_request_context(
                "/portal/hr/attendance/maternity-departures/new",
                method="POST",
                data={
                    "user_id": str(employee.id),
                    "start_day": "2026-09-01",
                    "note": "عودة من إجازة أمومة",
                },
            ):
                login_user(editor)
                response = hr_maternity_departure_new()
                self.assertEqual(response.status_code, 302)
                logout_user()

            maternity_departure = HRAttendanceSpecialCase.query.filter_by(
                kind="MATERNITY_DEPARTURE"
            ).one()
            self.assertEqual(maternity_departure.day, "2026-09-01")
            self.assertEqual(maternity_departure.day_to, "2027-08-31")
            self.assertEqual(maternity_departure.approval_status, "PENDING")
            self.assertFalse(maternity_departure.applied)
            self.assertEqual(_summary_compute_one(employee.id, "2026-09-01")["early_leave_minutes"], 60)

            with self.app.test_request_context(
                f"/portal/hr/attendance/maternity-departures/{maternity_departure.id}/review",
                method="POST",
                data={"action": "approve", "approval_note": "تمت المراجعة"},
            ):
                login_user(approver)
                response = hr_maternity_departure_review(maternity_departure.id)
                self.assertEqual(response.status_code, 302)
                logout_user()

        maternity_departure = db.session.get(HRAttendanceSpecialCase, maternity_departure.id)
        summary = AttendanceDailySummary.query.filter_by(user_id=employee.id, day="2026-09-01").one()
        self.assertEqual(maternity_departure.approval_status, "APPROVED")
        self.assertTrue(maternity_departure.applied)
        self.assertEqual(summary.early_leave_minutes, 0)
        self.assertEqual(AttendanceDailySummary.query.count(), 1)

    def test_maternity_leave_uses_regular_leave_workflow_and_allows_shorter_period(self):
        employee = User(email="employee@example.test", name="Employee", password_hash="x", role="USER")
        db.session.add(employee)
        db.session.flush()
        maternity_type = HRLeaveType(
            code="M",
            name_ar="إجازة أمومة",
            max_days=30,
            is_active=True,
        )
        db.session.add_all((
            maternity_type,
            UserPermission(user_id=employee.id, key="PORTAL_READ", is_allowed=True),
            UserPermission(user_id=employee.id, key="HR_READ", is_allowed=True),
            UserPermission(user_id=employee.id, key="HR_REQUESTS_CREATE", is_allowed=True),
            AttendanceEvent(user_id=employee.id, event_dt=datetime(2026, 9, 1, 8, 0), event_type="IN"),
            AttendanceEvent(user_id=employee.id, event_dt=datetime(2026, 9, 1, 14, 0), event_type="OUT"),
        ))
        db.session.commit()

        with self.app.test_request_context(
            "/portal/hr/me/leaves/new",
            method="POST",
            data={
                "leave_type_id": str(maternity_type.id),
                "start_date": "2026-09-01",
                "end_date": "2027-02-28",
                "note": "مغادرة أمومة",
            },
        ):
            login_user(employee)
            response = hr_leave_request_new()
            self.assertEqual(response.status_code, 302)
            logout_user()

        leave_request = HRLeaveRequest.query.one()
        self.assertEqual(leave_request.status, "SUBMITTED")
        self.assertEqual(leave_request.start_date, "2026-09-01")
        self.assertEqual(leave_request.end_date, "2027-02-28")
        self.assertTrue(
            HRRequestApprovalStep.query.filter_by(
                request_kind="LEAVE",
                request_id=leave_request.id,
            ).count()
        )
        self.assertIsNone(
            _maternity_leave_period_error(
                maternity_type,
                datetime(2026, 9, 1).date(),
                datetime(2027, 2, 28).date(),
            )
        )
        self.assertIsNotNone(
            _maternity_leave_period_error(
                maternity_type,
                datetime(2026, 9, 1).date(),
                datetime(2027, 9, 2).date(),
            )
        )

        schedule = SimpleNamespace(
            id=None,
            kind="FIXED",
            start_time="08:00",
            end_time="15:00",
            break_minutes=0,
            grace_minutes=0,
            start_grace_minutes=None,
            end_grace_minutes=None,
            overtime_threshold_minutes=0,
        )
        with patch("portal.routes._effective_schedule_for_user", return_value=schedule):
            self.assertEqual(_summary_compute_one(employee.id, "2026-09-01")["early_leave_minutes"], 60)
            leave_request.status = "APPROVED"
            db.session.commit()
            self.assertEqual(_summary_compute_one(employee.id, "2026-09-01")["early_leave_minutes"], 0)


class AttendanceAbsenceCandidatesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            SECRET_KEY="attendance-absence-test",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(cls.app)
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

    def test_candidates_exclude_punched_and_leave_recorded_employees(self):
        absent = User(email="absent@example.test", name="Absent", password_hash="x", role="USER")
        punched = User(email="punched@example.test", name="Punched", password_hash="x", role="USER")
        on_leave = User(email="leave@example.test", name="Leave", password_hash="x", role="USER")
        secretary_general = User(
            email="secretary@example.test",
            name="Secretary General",
            password_hash="x",
            role="GENERAL-SECRETARY",
        )
        unmapped = User(email="unmapped@example.test", name="Unmapped", password_hash="x", role="USER")
        db.session.add_all((absent, punched, on_leave, secretary_general, unmapped))
        db.session.flush()
        db.session.add_all((
            EmployeeFile(user_id=absent.id, timeclock_code="1001"),
            EmployeeFile(user_id=punched.id, timeclock_code="1002"),
            EmployeeFile(user_id=on_leave.id, timeclock_code="1003"),
            EmployeeFile(user_id=secretary_general.id, timeclock_code="1004"),
            EmployeeFile(user_id=unmapped.id),
        ))
        leave_type = HRLeaveType(code="ANNUAL", name_ar="Annual", is_active=True)
        db.session.add(leave_type)
        db.session.flush()
        db.session.add(AttendanceEvent(
            user_id=punched.id,
            event_dt=datetime(2026, 9, 2, 8, 0),
            event_type="IN",
        ))
        db.session.add(HRLeaveRequest(
            user_id=on_leave.id,
            leave_type_id=leave_type.id,
            start_date="2026-09-02",
            end_date="2026-09-02",
            status="SUBMITTED",
        ))
        db.session.commit()

        rows, excluded_reason = _attendance_absence_candidates("2026-09-02")

        self.assertIsNone(excluded_reason)
        self.assertEqual([row.user_id for row in rows], [absent.id])

    def test_maternity_leave_does_not_hide_an_employee_from_absence(self):
        employee = User(email="employee@example.test", name="Employee", password_hash="x", role="USER")
        db.session.add(employee)
        db.session.flush()
        maternity_type = HRLeaveType(code="M", name_ar="إجازة أمومة", is_active=True)
        db.session.add_all((
            maternity_type,
            EmployeeFile(user_id=employee.id, timeclock_code="1001"),
        ))
        db.session.flush()
        db.session.add(HRLeaveRequest(
            user_id=employee.id,
            leave_type_id=maternity_type.id,
            start_date="2026-09-02",
            end_date="2027-02-28",
            status="APPROVED",
        ))
        db.session.commit()

        rows, excluded_reason = _attendance_absence_candidates("2026-09-02")

        self.assertIsNone(excluded_reason)
        self.assertEqual([row.user_id for row in rows], [employee.id])

    def test_daily_attendance_query_excludes_explicit_absence_rows(self):
        present = User(email="present@example.test", name="Present", password_hash="x", role="USER")
        absent = User(email="absent@example.test", name="Absent", password_hash="x", role="USER")
        db.session.add_all((present, absent))
        db.session.flush()
        db.session.add_all((
            AttendanceDailySummary(
                user_id=present.id,
                day="2026-09-02",
                status="OK",
            ),
            AttendanceDailySummary(
                user_id=absent.id,
                day="2026-09-02",
                status="ABSENT",
            ),
        ))
        db.session.commit()

        rows = _attendance_daily_without_absences(
            AttendanceDailySummary.query
        ).all()

        self.assertEqual([row.user_id for row in rows], [present.id])


if __name__ == "__main__":
    unittest.main()
