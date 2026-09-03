import unittest
from datetime import date

from flask import Flask
from flask_login import LoginManager, login_user, logout_user

from extensions import db
from models import (
    HROfficialOccasion,
    HRLeaveRequest,
    HRLeaveType,
    SystemSetting,
    User,
    UserPermission,
)
from portal import portal_bp
from portal.routes import _calculate_leave_days, _leave_used_days_as_of, hr_leave_type_new


class LeaveTypePolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            SECRET_KEY="leave-type-policy-test",
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
        # Friday and Saturday are weekly days off (weekday indexes 4 and 5).
        db.session.add(SystemSetting(key="HR_WEEKLY_HOLIDAYS_MASK", value=str((1 << 4) | (1 << 5))))
        db.session.add(HROfficialOccasion(title="عطلة رسمية", day="2026-09-06", is_day_off=True))
        db.session.commit()

    def test_duration_policy_applies_weekends_and_official_holidays_independently(self):
        calendar = HRLeaveType(
            code="CAL",
            name_ar="تقويمية",
            day_count_basis="CALENDAR_DAYS",
            exclude_official_holidays=False,
        )
        calendar_without_official = HRLeaveType(
            code="CAL-OFF",
            name_ar="تقويمية دون الرسمية",
            day_count_basis="CALENDAR_DAYS",
            exclude_official_holidays=True,
        )
        working = HRLeaveType(
            code="WORK",
            name_ar="أيام عمل",
            day_count_basis="WORKING_DAYS",
            exclude_official_holidays=False,
        )
        working_without_official = HRLeaveType(
            code="WORK-OFF",
            name_ar="أيام عمل دون الرسمية",
            day_count_basis="WORKING_DAYS",
            exclude_official_holidays=True,
        )
        db.session.add_all((calendar, calendar_without_official, working, working_without_official))
        db.session.commit()

        start, end = "2026-09-03", "2026-09-07"  # Thu–Mon; official holiday is Sunday.
        self.assertEqual(_calculate_leave_days(calendar, start, end), 5)
        self.assertEqual(_calculate_leave_days(calendar_without_official, start, end), 4)
        self.assertEqual(_calculate_leave_days(working, start, end), 3)
        self.assertEqual(_calculate_leave_days(working_without_official, start, end), 2)

    def test_non_deductible_leave_never_consumes_a_leave_balance(self):
        employee = User(email="policy@example.test", name="Policy Employee", password_hash="x", role="USER")
        deductible = HRLeaveType(
            code="ANNUAL",
            name_ar="سنوية",
            deduct_from_balance=True,
            day_count_basis="CALENDAR_DAYS",
        )
        deductible_working_days = HRLeaveType(
            code="ANNUAL-WORK",
            name_ar="سنوية أيام عمل",
            deduct_from_balance=True,
            day_count_basis="WORKING_DAYS",
        )
        non_deductible = HRLeaveType(
            code="MATERNITY",
            name_ar="أمومة",
            deduct_from_balance=False,
            day_count_basis="CALENDAR_DAYS",
        )
        db.session.add_all((employee, deductible, deductible_working_days, non_deductible))
        db.session.flush()
        db.session.add_all((
            HRLeaveRequest(
                user_id=employee.id,
                leave_type_id=deductible.id,
                start_date="2026-09-03",
                end_date="2026-09-07",
                days=5,
                status="APPROVED",
            ),
            HRLeaveRequest(
                user_id=employee.id,
                leave_type_id=deductible_working_days.id,
                start_date="2026-09-03",
                end_date="2026-09-07",
                days=3,
                status="APPROVED",
            ),
            HRLeaveRequest(
                user_id=employee.id,
                leave_type_id=non_deductible.id,
                start_date="2026-09-03",
                end_date="2026-09-07",
                days=5,
                status="APPROVED",
            ),
        ))
        db.session.commit()

        as_of = date(2026, 9, 7)
        self.assertEqual(_leave_used_days_as_of(employee.id, deductible.id, 2026, as_of), 5.0)
        self.assertEqual(_leave_used_days_as_of(employee.id, deductible_working_days.id, 2026, as_of), 3.0)
        self.assertEqual(_leave_used_days_as_of(employee.id, non_deductible.id, 2026, as_of), 0.0)

    def test_master_data_persists_each_leave_policy(self):
        manager = User(email="manager@example.test", name="HR Manager", password_hash="x", role="HR")
        db.session.add(manager)
        db.session.flush()
        db.session.add(UserPermission(user_id=manager.id, key="HR_MASTERDATA_MANAGE", is_allowed=True))
        db.session.commit()

        with self.app.test_request_context(
            "/portal/hr/masterdata/leave-type/new",
            method="POST",
            data={
                "code": "MATERNITY",
                "name_ar": "إجازة أمومة",
                "requires_approval": "1",
                "max_days": "90",
                "deduct_from_balance": "0",
                "day_count_basis": "CALENDAR_DAYS",
                "exclude_official_holidays": "1",
            },
        ):
            login_user(manager)
            response = hr_leave_type_new()
            self.assertEqual(response.status_code, 302)
            logout_user()

        leave_type = HRLeaveType.query.filter_by(code="MATERNITY").one()
        self.assertFalse(leave_type.deduct_from_balance)
        self.assertEqual(leave_type.day_count_basis, "CALENDAR_DAYS")
        self.assertTrue(leave_type.exclude_official_holidays)
