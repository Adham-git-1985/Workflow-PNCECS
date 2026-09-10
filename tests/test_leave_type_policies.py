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
from portal.routes import (
    _calculate_leave_days,
    _leave_balance_owner_id,
    _leave_duration_limit_messages,
    _leave_entitlement_days,
    _leave_type_deducts_from_balance,
    _leave_type_owns_balance,
    _leave_used_days_as_of,
    hr_leaves_admin_new,
    hr_leave_type_new,
)


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

    def test_external_leave_consumes_the_annual_balance(self):
        employee = User(email="external@example.test", name="External Employee", password_hash="x", role="USER")
        annual = HRLeaveType(
            code="A",
            name_ar="إجازة سنوية",
            default_balance_days=30,
            deduct_from_balance=True,
            day_count_basis="CALENDAR_DAYS",
        )
        db.session.add_all((employee, annual))
        db.session.flush()
        external = HRLeaveType(
            code="O",
            name_ar="إجازة خارجية",
            deduct_from_balance=True,
            balance_source_leave_type_id=annual.id,
            day_count_basis="CALENDAR_DAYS",
            is_external=True,
        )
        db.session.add(external)
        db.session.flush()
        db.session.add_all((
            HRLeaveRequest(
                user_id=employee.id,
                leave_type_id=annual.id,
                start_date="2026-01-01",
                end_date="2026-01-02",
                days=2,
                leave_place="EXTERNAL",
                status="APPROVED",
            ),
            HRLeaveRequest(
                user_id=employee.id,
                leave_type_id=external.id,
                start_date="2026-01-03",
                end_date="2026-01-05",
                days=3,
                leave_place="EXTERNAL",
                status="APPROVED",
            ),
        ))
        db.session.commit()

        self.assertTrue(_leave_type_deducts_from_balance(external))
        self.assertTrue(_leave_type_owns_balance(annual))
        self.assertFalse(_leave_type_owns_balance(external))
        self.assertEqual(_leave_balance_owner_id(external.id), annual.id)
        self.assertEqual(_leave_entitlement_days(employee.id, external, 2026), 30.0)
        self.assertEqual(_leave_used_days_as_of(employee.id, annual.id, 2026, date(2026, 1, 31)), 5.0)
        self.assertEqual(_leave_used_days_as_of(employee.id, external.id, 2026, date(2026, 1, 31)), 5.0)

    def test_maternity_and_hajj_maximum_days_are_enforced(self):
        maternity = HRLeaveType(code="M", name_ar="إجازة أمومة", max_days=90)
        hajj = HRLeaveType(code="H", name_ar="إجازة حج", max_days=30)

        self.assertEqual(_leave_duration_limit_messages(maternity, 90), (None, None))
        self.assertIn("90", _leave_duration_limit_messages(maternity, 91)[0])
        self.assertEqual(_leave_duration_limit_messages(hajj, 30), (None, None))
        self.assertIn("30", _leave_duration_limit_messages(hajj, 31)[0])

    def test_admin_entry_cannot_bypass_hajj_maximum(self):
        manager = User(email="leave-admin@example.test", name="Leave Admin", password_hash="x", role="HR")
        employee = User(email="hajj@example.test", name="Hajj Employee", password_hash="x", role="USER")
        hajj = HRLeaveType(
            code="H",
            name_ar="إجازة حج",
            max_days=30,
            day_count_basis="CALENDAR_DAYS",
            deduct_from_balance=False,
        )
        db.session.add_all((manager, employee, hajj))
        db.session.flush()
        db.session.add(UserPermission(user_id=manager.id, key="HR_MASTERDATA_MANAGE", is_allowed=True))
        db.session.commit()

        with self.app.test_request_context(
            "/portal/hr/leaves/admin/new",
            method="POST",
            data={
                "user_id": str(employee.id),
                "leave_type_id": str(hajj.id),
                "start_date": "2026-01-01",
                "end_date": "2026-01-31",
                "leave_place": "EXTERNAL",
            },
        ):
            login_user(manager)
            response = hr_leaves_admin_new()
            self.assertEqual(response.status_code, 302)
            logout_user()

        self.assertEqual(HRLeaveRequest.query.filter_by(user_id=employee.id).count(), 0)

    def test_master_data_can_link_external_type_to_annual_balance(self):
        manager = User(email="balance-admin@example.test", name="Balance Admin", password_hash="x", role="HR")
        annual = HRLeaveType(
            code="A",
            name_ar="إجازة سنوية",
            default_balance_days=30,
            deduct_from_balance=True,
        )
        db.session.add_all((manager, annual))
        db.session.flush()
        db.session.add(UserPermission(user_id=manager.id, key="HR_MASTERDATA_MANAGE", is_allowed=True))
        db.session.commit()

        with self.app.test_request_context(
            "/portal/hr/masterdata/leave-type/new",
            method="POST",
            data={
                "code": "O",
                "name_ar": "إجازة خارجية",
                "requires_approval": "1",
                "deduct_from_balance": "1",
                "balance_source_leave_type_id": str(annual.id),
                "day_count_basis": "CALENDAR_DAYS",
                "is_external": "1",
            },
        ):
            login_user(manager)
            response = hr_leave_type_new()
            self.assertEqual(response.status_code, 302)
            logout_user()

        external = HRLeaveType.query.filter_by(code="O").one()
        self.assertEqual(external.balance_source_leave_type_id, annual.id)
        self.assertTrue(external.deduct_from_balance)

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
        self.assertEqual(leave_type.max_days, 90)
        self.assertFalse(leave_type.deduct_from_balance)
        self.assertEqual(leave_type.day_count_basis, "CALENDAR_DAYS")
        self.assertTrue(leave_type.exclude_official_holidays)
