import os
import unittest
from datetime import datetime, timedelta

from flask import Flask
from flask_login import LoginManager, login_user

from extensions import db
from models import (
    ArchivedFile,
    AttendanceDailySummary,
    EmployeeFile,
    HRAttendanceDelayRequest,
    Notification,
    OrgNode,
    OrgNodeManager,
    OrgNodeType,
    RequestEscalation,
    User,
    WorkflowInstanceStep,
    WorkflowStepTask,
)
from portal import portal_bp
from workflow import workflow_bp
from workflow.engine import decide_step
from services.attendance_delay_workflow import (
    attendance_delay_hr_affairs_manager_user_ids,
    attendance_delay_hr_department_manager_user_ids,
    attendance_delay_hr_general_director_user_ids,
    process_pending_delay_response_alerts,
)
from services.hr_request_workflow import secretary_general_user_ids


class AttendanceDelayWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            SECRET_KEY="attendance-delay-workflow-test",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            WTF_CSRF_ENABLED=False,
        )
        db.init_app(cls.app)
        cls.login_manager = LoginManager(cls.app)

        @cls.login_manager.user_loader
        def _load_user(user_id):
            return db.session.get(User, int(user_id))

        @cls.app.route("/_test/login/<int:user_id>")
        def _test_login(user_id):
            login_user(db.session.get(User, user_id))
            return "ok"

        cls.app.register_blueprint(workflow_bp)
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
        self.client = self.app.test_client()
        self.admin = User(email="delay-admin@example.test", name="Delay Admin", password_hash="x", role="SUPER_ADMIN")
        self.hr = User(email="delay-hr@example.test", name="Delay HR", password_hash="x", role="HR")
        self.hr_department_manager = User(
            email="delay-hr-department-manager@example.test",
            name="HR Department Manager",
            password_hash="x",
            role="HR_DEPARTMENT_MANAGER",
            job_title="مدير دائرة الموارد البشرية",
        )
        self.hr_general_director = User(
            email="delay-hr-general-director@example.test",
            name="HR General Director",
            password_hash="x",
            role="HR_GENERAL_DIRECTOR",
            job_title="مدير عام الإدارة العامة للموارد الإدارية والمالية",
        )
        self.hr_affairs_manager = User(
            email="delay-hr-affairs-manager@example.test",
            name="HR Affairs Manager",
            password_hash="x",
            role="HR_MANAGER",
            job_title="مدير الشؤون البشرية",
        )
        self.secretary = User(
            email="delay-secretary@example.test",
            name="Delay Secretary",
            password_hash="x",
            role="GENERAL_SECRETARY",
        )
        self.manager = User(email="delay-manager@example.test", name="Delay Manager", password_hash="x", role="MANAGER")
        self.employee = User(email="delay-employee@example.test", name="Delay Employee", password_hash="x", role="EMPLOYEE")
        db.session.add_all((
            self.admin,
            self.hr,
            self.hr_department_manager,
            self.hr_general_director,
            self.hr_affairs_manager,
            self.secretary,
            self.manager,
            self.employee,
        ))
        db.session.flush()
        db.session.add(
            EmployeeFile(
                user_id=self.employee.id,
                direct_manager_user_id=self.manager.id,
                employee_no="D-100",
            )
        )
        self.summary = AttendanceDailySummary(
            user_id=self.employee.id,
            day="2026-09-23",
            first_in=datetime(2026, 9, 23, 8, 37),
            late_minutes=37,
            early_leave_minutes=0,
            status="OK",
        )
        db.session.add(self.summary)
        db.session.commit()

    def tearDown(self):
        for archived in list(ArchivedFile.query.all()):
            path = getattr(archived, "file_path", None)
            if path and os.path.exists(path):
                os.remove(path)
        db.session.rollback()

    def _login(self, user):
        self.client.get(f"/_test/login/{user.id}")

    def _start(self):
        self._login(self.admin)
        response = self.client.post(
            f"/portal/hr/reports/attendance/delay/{self.summary.id}/start",
        )
        self.assertEqual(response.status_code, 302)
        case = HRAttendanceDelayRequest.query.one()
        return case, case.workflow_request

    def test_employee_response_manager_hr_secretary_and_hr_affairs_final_path(self):
        case, request_row = self._start()
        steps = WorkflowInstanceStep.query.filter_by(
            instance_id=request_row.workflow_instance.id,
        ).order_by(WorkflowInstanceStep.step_order.asc()).all()
        self.assertEqual([step.approver_user_id for step in steps[:2]], [self.employee.id, self.manager.id])
        self.assertEqual(len(steps), 5)
        self.assertEqual(steps[2].mode, "PARALLEL_SYNC")
        self.assertEqual(steps[2].approver_role, "ATTENDANCE_DELAY_HR_PARALLEL")
        self.assertEqual(steps[2].approver_user_id, self.hr_department_manager.id)
        self.assertEqual(steps[3].approver_user_id, self.secretary.id)
        self.assertEqual(steps[4].approver_user_id, self.hr_affairs_manager.id)

        self._login(self.employee)
        response = self.client.post(
            f"/portal/hr/reports/attendance/delay/{case.id}/respond",
            data={"case_kind": "DELAY", "reason": "ظرف طارئ", "has_document": "NO"},
        )
        self.assertEqual(response.status_code, 302)
        db.session.refresh(case)
        self.assertIsNotNone(case.employee_responded_at)
        self.assertEqual(request_row.workflow_instance.current_step_order, 2)

        decide_step(
            request_row.id,
            2,
            self.manager.id,
            "APPROVED",
            note="موافق",
            effective_user_id=self.manager.id,
        )
        db.session.commit()
        self.assertEqual(request_row.workflow_instance.current_step_order, 3)
        self.assertEqual(
            {
                task.assignee_user_id
                for task in WorkflowStepTask.query.filter_by(step_order=3).all()
            },
            {self.hr_department_manager.id, self.hr_general_director.id},
        )

        decide_step(
            request_row.id,
            3,
            self.hr_department_manager.id,
            "APPROVED",
            note="موافقة مدير الدائرة",
            effective_user_id=self.hr_department_manager.id,
        )
        db.session.commit()
        self.assertEqual(request_row.workflow_instance.current_step_order, 3)

        decide_step(
            request_row.id,
            3,
            self.hr_general_director.id,
            "APPROVED",
            note="موافقة المدير العام",
            effective_user_id=self.hr_general_director.id,
        )
        db.session.commit()
        self.assertEqual(request_row.workflow_instance.current_step_order, 4)

        decide_step(
            request_row.id,
            4,
            self.secretary.id,
            "APPROVED",
            note="اعتماد الأمين العام",
            effective_user_id=self.secretary.id,
        )
        db.session.commit()
        self.assertEqual(request_row.workflow_instance.current_step_order, 5)

        decide_step(
            request_row.id,
            5,
            self.hr_affairs_manager.id,
            "APPROVED",
            note="الاعتماد النهائي",
            effective_user_id=self.hr_affairs_manager.id,
        )
        db.session.commit()
        self.assertEqual(request_row.status, "APPROVED")
        self.assertTrue(request_row.workflow_instance.is_completed)
        self.assertEqual(case.final_status, "APPROVED")

    def test_attendance_delay_hr_resolvers_match_the_configured_roles(self):
        self.assertIn(
            self.hr_department_manager.id,
            attendance_delay_hr_department_manager_user_ids(),
        )
        self.assertIn(
            self.hr_general_director.id,
            attendance_delay_hr_general_director_user_ids(),
        )
        self.assertIn(
            self.hr_affairs_manager.id,
            attendance_delay_hr_affairs_manager_user_ids(),
        )

    def test_two_hour_alert_is_one_time_and_creates_escalation(self):
        case, request_row = self._start()
        case.response_due_at = datetime.utcnow() - timedelta(minutes=1)
        db.session.commit()

        self.assertEqual(process_pending_delay_response_alerts(), 1)
        db.session.commit()
        db.session.refresh(case)
        self.assertIsNotNone(case.overdue_notified_at)
        self.assertEqual(process_pending_delay_response_alerts(), 0)
        self.assertTrue(Notification.query.filter_by(type="ATTENDANCE_DELAY_OVERDUE").count() >= 2)
        self.assertEqual(RequestEscalation.query.filter_by(request_id=request_row.id).count(), 1)

    def test_start_removes_orphan_delay_row_before_reusing_workflow_id(self):
        orphan = HRAttendanceDelayRequest(
            workflow_request_id=999999,
            attendance_summary_id=self.summary.id,
            employee_id=self.employee.id,
            initiated_by_id=self.admin.id,
            delay_day="2026-09-22",
            created_at=datetime.utcnow(),
        )
        db.session.add(orphan)
        db.session.commit()

        case, request_row = self._start()

        self.assertEqual(HRAttendanceDelayRequest.query.count(), 1)
        self.assertEqual(case.workflow_request_id, request_row.id)
        self.assertNotEqual(case.workflow_request_id, 999999)

    def test_secretary_general_resolver_accepts_title_and_org_node_labels(self):
        title_secretary = User(
            email="delay-title-secretary@example.test",
            name="Title Secretary",
            password_hash="x",
            role="ADMIN",
            job_title="امين عام المجلس",
        )
        node_secretary = User(
            email="delay-node-secretary@example.test",
            name="Node Secretary",
            password_hash="x",
            role="ADMIN",
        )
        node_type = OrgNodeType(
            code="ORGANIZATION",
            name_ar="منظمة",
            name_en="Organization",
        )
        secretary_node = OrgNode(
            type=node_type,
            name_ar="الأمين العام",
            name_en="Secretary General",
        )
        db.session.add_all((title_secretary, node_secretary, node_type, secretary_node))
        db.session.flush()
        db.session.add(OrgNodeManager(node_id=secretary_node.id, manager_user_id=node_secretary.id))
        db.session.commit()

        resolved_ids = secretary_general_user_ids()
        self.assertIn(self.secretary.id, resolved_ids)
        self.assertIn(title_secretary.id, resolved_ids)
        self.assertIn(node_secretary.id, resolved_ids)


if __name__ == "__main__":
    unittest.main()
