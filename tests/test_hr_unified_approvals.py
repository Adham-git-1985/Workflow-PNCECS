import unittest

from flask import Flask
from extensions import db
from models import (
    EmployeeFile,
    HRAttendanceScheduleDay,
    HRAttendanceSchedulePlan,
    HRAttendanceSpecialCase,
    User,
    UserPermission,
)
from portal.routes import (
    HR_APPROVALS_VIEW,
    _attendance_approval_inbox_rows,
    _hr_approvals_can_open,
)


class UnifiedApprovalsInboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="unified-approvals-test",
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
            email="unified-employee@example.test",
            name="الموظف",
            password_hash="x",
            role="EMPLOYEE",
        )
        self.manager = User(
            email="unified-manager@example.test",
            name="المدير",
            password_hash="x",
            role="MANAGER",
        )
        self.affairs_manager = User(
            email="unified-affairs@example.test",
            name="مدير الشؤون الإدارية",
            password_hash="x",
            role="HR_MANAGER",
        )
        self.viewer = User(
            email="unified-viewer@example.test",
            name="مراقب الموافقات",
            password_hash="x",
            role="EMPLOYEE",
        )
        db.session.add_all((
            self.employee,
            self.manager,
            self.affairs_manager,
            self.viewer,
        ))
        db.session.flush()
        db.session.add_all((
            EmployeeFile(
                user_id=self.employee.id,
                direct_manager_user_id=self.manager.id,
            ),
            UserPermission(
                user_id=self.viewer.id,
                key=HR_APPROVALS_VIEW,
                is_allowed=True,
            ),
        ))
        db.session.commit()

    def test_schedule_change_is_available_to_direct_manager(self):
        plan = HRAttendanceSchedulePlan(
            user_id=self.employee.id,
            manager_user_id=self.manager.id,
            period_start="2032-01-04",
            period_end="2032-01-10",
            version_no=1,
            status="SUBMITTED",
            request_type="CHANGE_REQUEST",
            employee_note="تغيير وقت الحضور",
        )
        db.session.add(plan)
        db.session.flush()
        db.session.add(HRAttendanceScheduleDay(
            plan_id=plan.id,
            work_date="2032-01-04",
            day_type="WORK",
            start_time="09:00",
            end_time="16:00",
        ))
        db.session.commit()

        with self.app.test_request_context("/portal/hr/approvals"):
            schedule_rows, manual_rows = _attendance_approval_inbox_rows(user=self.manager)

        self.assertEqual([row.id for row in schedule_rows], [plan.id])
        self.assertEqual(manual_rows, [])

    def test_daily_edit_is_available_to_administrative_affairs_reviewer(self):
        correction = HRAttendanceSpecialCase(
            user_id=self.employee.id,
            day="2032-01-04",
            day_to="2032-01-04",
            kind="MANUAL_ATTENDANCE",
            start_time="08:05",
            approval_status="PENDING",
            applied=False,
            created_by_id=self.employee.id,
        )
        db.session.add(correction)
        db.session.commit()

        with self.app.test_request_context("/portal/hr/approvals"):
            schedule_rows, manual_rows = _attendance_approval_inbox_rows(user=self.affairs_manager)

        self.assertEqual(schedule_rows, [])
        self.assertEqual([row.id for row in manual_rows], [correction.id])
        self.assertTrue(manual_rows[0].can_review)

    def test_daily_edit_with_legacy_first_approval_moves_to_secretary_general(self):
        secretary = User(
            email="unified-secretary@example.test",
            name="Secretary General",
            password_hash="x",
            role="GENERAL_SECRETARY",
        )
        correction = HRAttendanceSpecialCase(
            user_id=self.employee.id,
            day="2032-01-04",
            day_to="2032-01-04",
            kind="MANUAL_ATTENDANCE",
            start_time="08:05",
            approval_status="PENDING",
            applied=False,
            # Older records can have a first-stage reviewer without the
            # timestamp that newer versions also persist.
            approved_by_id=self.affairs_manager.id,
            created_by_id=self.employee.id,
        )
        db.session.add_all((secretary, correction))
        db.session.commit()

        with self.app.test_request_context("/portal/hr/approvals"):
            schedule_rows, manual_rows = _attendance_approval_inbox_rows(user=secretary)

        self.assertEqual(schedule_rows, [])
        self.assertEqual([row.id for row in manual_rows], [correction.id])
        self.assertEqual(manual_rows[0].review_stage, "SECRETARY_GENERAL")
        self.assertTrue(manual_rows[0].can_review)

    def test_pending_schedule_remains_visible_to_a_manager_who_already_reviewed_it(self):
        plan = HRAttendanceSchedulePlan(
            user_id=self.employee.id,
            manager_user_id=self.manager.id,
            manager_approved_by_id=self.manager.id,
            period_start="2032-01-04",
            period_end="2032-01-10",
            version_no=1,
            status="MANAGER_APPROVED",
            request_type="CHANGE_REQUEST",
        )
        db.session.add(plan)
        db.session.commit()

        with self.app.test_request_context("/portal/hr/approvals?status=SUBMITTED"):
            schedule_rows, manual_rows = _attendance_approval_inbox_rows("SUBMITTED", user=self.manager)

        self.assertEqual([row.id for row in schedule_rows], [plan.id])
        self.assertIsNone(schedule_rows[0].review_stage)
        self.assertEqual(manual_rows, [])

    def test_pending_daily_edit_remains_visible_to_its_creator(self):
        correction = HRAttendanceSpecialCase(
            user_id=self.employee.id,
            day="2032-01-04",
            day_to="2032-01-04",
            kind="MANUAL_ATTENDANCE",
            start_time="08:05",
            approval_status="PENDING",
            applied=False,
            created_by_id=self.manager.id,
        )
        db.session.add(correction)
        db.session.commit()

        with self.app.test_request_context("/portal/hr/approvals?status=SUBMITTED"):
            schedule_rows, manual_rows = _attendance_approval_inbox_rows("SUBMITTED", user=self.manager)

        self.assertEqual(schedule_rows, [])
        self.assertEqual([row.id for row in manual_rows], [correction.id])
        self.assertFalse(manual_rows[0].can_review)

    def test_page_permission_can_be_assigned_without_granting_an_action_stage(self):
        self.assertTrue(_hr_approvals_can_open(self.viewer))
        with self.app.test_request_context("/portal/hr/approvals"):
            schedule_rows, manual_rows = _attendance_approval_inbox_rows(user=self.viewer)
        self.assertEqual(schedule_rows, [])
        self.assertEqual(manual_rows, [])


if __name__ == "__main__":
    unittest.main()
