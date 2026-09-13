from datetime import date
import unittest
import inspect
from types import SimpleNamespace
from unittest.mock import patch
from html.parser import HTMLParser
from pathlib import Path

from flask import Flask
from flask_login import LoginManager, login_user

from extensions import db
from models import AuditLog, HRTrainingCourse, HRTrainingEnrollment, HRTrainingProgram, User
from portal import portal_bp
from portal.routes import _period_within_month_limit
from portal.routes import (
    _training_can_manage,
    _manager_ids_for_employee,
    _save_mission_attachment,
    hr_training_program_enrollments,
)


class WorkflowReviewRegressionTests(unittest.TestCase):
    def test_read_permissions_do_not_grant_training_management(self):
        for permission in ('HR_REPORTS_VIEW', 'HR_REQUESTS_VIEW_ALL'):
            with self.subTest(permission=permission), patch('portal.routes.current_user', SimpleNamespace(has_perm=lambda key: key == permission)):
                self.assertFalse(_training_can_manage())
        with patch('portal.routes.current_user', SimpleNamespace(has_perm=lambda key: key == 'HR_EMPLOYEE_MANAGE')):
            self.assertTrue(_training_can_manage())

    def test_manager_resolution_excludes_requester(self):
        with patch('portal.routes.resolve_responsible_managers', return_value=[SimpleNamespace(id=7), SimpleNamespace(id=8)]):
            self.assertEqual(_manager_ids_for_employee(7), {8})

    def test_rejection_buttons_bypass_approval_only_browser_requirements(self):
        class Buttons(HTMLParser):
            def __init__(self):
                super().__init__()
                self.actions = {}

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'button' and attrs.get('name') == 'action':
                    self.actions[attrs.get('value')] = attrs

        for filename, actions in (
            ('training/requests.html', ('MANAGER_REJECT', 'HR_REJECT')),
            ('mission_requests_queue.html', ('HR_REJECT',)),
        ):
            parser = Buttons()
            parser.feed((Path(__file__).resolve().parents[1] / 'templates/portal/hr' / filename).read_text(encoding='utf-8'))
            for action in actions:
                self.assertIn('formnovalidate', parser.actions[action])

    def test_mission_attachment_is_staged_in_the_callers_transaction(self):
        self.assertNotIn('db.session.commit()', inspect.getsource(_save_mission_attachment))

    def test_training_enrollment_template_does_not_offer_direct_approval(self):
        template = (Path(__file__).resolve().parents[1] / 'templates/portal/hr/training/program_enrollments.html').read_text(encoding='utf-8')
        self.assertNotIn('<option value="APPROVED">', template)
        self.assertNotIn('<option value="COMPLETED">', template)


class StatutoryMissionTrainingDateTests(unittest.TestCase):
    def test_official_mission_period_is_limited_to_one_inclusive_calendar_month(self):
        self.assertTrue(_period_within_month_limit(date(2026, 1, 1), date(2026, 1, 31), 1))
        self.assertFalse(_period_within_month_limit(date(2026, 1, 1), date(2026, 2, 1), 1))

    def test_training_period_is_limited_to_eight_months_and_handles_leap_year(self):
        self.assertTrue(_period_within_month_limit(date(2024, 2, 1), date(2024, 9, 30), 8))
        self.assertFalse(_period_within_month_limit(date(2024, 2, 1), date(2024, 10, 1), 8))

    def test_month_end_clamps_to_shorter_month(self):
        self.assertTrue(_period_within_month_limit(date(2024, 1, 31), date(2024, 2, 28), 1))
        self.assertFalse(_period_within_month_limit(date(2024, 1, 31), date(2024, 2, 29), 1))


class TrainingEnrollmentAdminWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            SECRET_KEY='training-enrollment-workflow-test',
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
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
        self.admin = User(email='training-admin@example.test', name='Training Admin', password_hash='x', role='USER')
        self.employee = User(email='training-employee@example.test', name='Training Employee', password_hash='x', role='USER')
        course = HRTrainingCourse(name_ar='دورة اختبار')
        db.session.add_all((self.admin, self.employee, course))
        db.session.flush()
        self.program = HRTrainingProgram(course_id=course.id, start_date='2026-09-10', end_date='2026-09-12')
        db.session.add(self.program)
        db.session.flush()
        self.enrollment = HRTrainingEnrollment(
            program_id=self.program.id,
            user_id=self.employee.id,
            status='CANDIDATE',
            notes='سبب الترشيح الأصلي',
        )
        db.session.add(self.enrollment)
        db.session.commit()

    def _submit_admin_update(self, *, status, notes):
        with self.app.test_request_context(
            f'/portal/hr/training/programs/{self.program.id}/enrollments',
            method='POST',
            data={'action': 'update', 'enroll_id': self.enrollment.id, 'status': status, 'notes': notes},
        ):
            login_user(self.admin)
            with patch.object(User, 'has_perm', return_value=True), patch('portal.routes._training_can_manage', return_value=True):
                return hr_training_program_enrollments(self.program.id)

    def test_admin_cannot_bypass_approval_but_can_record_audited_withdrawal(self):
        response = self._submit_admin_update(status='HR_REVIEW', notes='محاولة تجاوز')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.session.get(HRTrainingEnrollment, self.enrollment.id).status, 'CANDIDATE')

        response = self._submit_admin_update(status='WITHDRAWN', notes='ألغي الترشيح لسبب تشغيلي')
        self.assertEqual(response.status_code, 302)
        enrollment = db.session.get(HRTrainingEnrollment, self.enrollment.id)
        self.assertEqual(enrollment.status, 'WITHDRAWN')
        self.assertIn('سبب الترشيح الأصلي', enrollment.notes)
        self.assertEqual(
            AuditLog.query.filter_by(action='HR_TRAINING_ADMIN_UPDATE', target_id=enrollment.id).count(),
            1,
        )


if __name__ == "__main__":
    unittest.main()
