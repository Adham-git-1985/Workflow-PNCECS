import unittest

from flask import Flask

from extensions import db
from models import EmployeeFile, HRAttendanceExemption, User
from portal import portal_bp
from portal.routes import (
    _attendance_absence_candidates,
    _attendance_exemption_reason,
    _can_manage_attendance_exemptions,
    _filtered_user_ids,
    _monthly_attendance_deduction_breakdown,
)


class AttendanceExemptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            SECRET_KEY="attendance-exemption-test",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(cls.app)
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

    def _seed_users(self):
        admin = User(
            email="admin@example.test",
            name="Admin",
            password_hash="x",
            role="ADMIN",
        )
        regular_user = User(
            email="regular@example.test",
            name="Regular employee",
            password_hash="x",
            role="USER",
        )
        exempt_user = User(
            email="exempt@example.test",
            name="Exempt employee",
            password_hash="x",
            role="USER",
        )
        super_admin = User(
            email="super-admin@example.test",
            name="Super admin",
            password_hash="x",
            role="SUPER_ADMIN",
        )
        db.session.add_all((admin, regular_user, exempt_user, super_admin))
        db.session.flush()
        db.session.add_all((
            EmployeeFile(
                user_id=regular_user.id,
                employee_no="1001",
                timeclock_code="1001",
            ),
            EmployeeFile(
                user_id=exempt_user.id,
                employee_no="1002",
                timeclock_code="1002",
            ),
            HRAttendanceExemption(
                user_id=exempt_user.id,
                created_by_id=admin.id,
            ),
        ))
        db.session.commit()
        return admin, regular_user, exempt_user, super_admin

    def test_only_admin_and_super_admin_can_manage_exemptions(self):
        admin, regular_user, _, super_admin = self._seed_users()

        self.assertTrue(_can_manage_attendance_exemptions(admin))
        self.assertTrue(_can_manage_attendance_exemptions(super_admin))
        self.assertFalse(_can_manage_attendance_exemptions(regular_user))

    def test_exempt_employee_is_removed_from_absence_and_deduction_inputs(self):
        _, regular_user, exempt_user, _ = self._seed_users()

        filtered_ids = _filtered_user_ids(exclude_attendance_exempt=True)
        self.assertIn(regular_user.id, filtered_ids)
        self.assertNotIn(exempt_user.id, filtered_ids)

        absence_rows, reason = _attendance_absence_candidates("2026-09-21")
        self.assertIsNone(reason)
        self.assertEqual([row.user_id for row in absence_rows], [regular_user.id])

        breakdown = _monthly_attendance_deduction_breakdown(
            exempt_user.id,
            2026,
            9,
            420,
            360,
        )
        self.assertEqual(breakdown["chargeable_minutes"], 0)
        self.assertEqual(breakdown["absent_days"], 0)
        self.assertTrue(breakdown["details"]["summary"]["attendance_exempt"])

    def test_exemption_is_applied_before_daily_attendance_calculation(self):
        _, _, exempt_user, _ = self._seed_users()

        self.assertEqual(
            _attendance_exemption_reason(exempt_user.id, "2026-09-21"),
            "ATTENDANCE_EXEMPT",
        )


if __name__ == "__main__":
    unittest.main()
