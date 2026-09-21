import unittest
import json
from datetime import date, datetime, timedelta

from flask import Flask
from flask_login import LoginManager, login_user, logout_user
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from werkzeug.exceptions import Forbidden

from extensions import db
from models import (
    AttendanceDailySummary,
    EmployeeFile,
    HRAttendanceDeductionItem,
    HRAttendanceDeductionRun,
    HRAttendanceSpecialCase,
    HRLeaveBalance,
    HRLeaveBalanceAdjustment,
    HRLeaveRolloverDecision,
    HRLeaveRequest,
    HRLeaveType,
    HROfficialMission,
    HRPermissionRequest,
    HRPermissionType,
    SystemSetting,
    User,
    UserPermission,
    WorkAssignment,
    WorkPolicy,
    WorkSchedule,
)
from portal.routes import (
    HR_LEAVE_BALANCES_MANAGE,
    HR_REPORTS_VIEW,
    _compensatory_leave_balance_error,
    _activate_due_leave_rollover_decisions,
    _annual_bucket_rows,
    _leave_balance_display_values,
    _leave_balance_usage_by_year,
    _leave_entitlement_days,
    _leave_rollover_rows,
    _save_leave_rollover_decision,
    _leave_used_days_as_of,
    _monthly_attendance_deduction_breakdown,
    _one_time_leave_error,
    hr_deduction_approve,
    hr_deduction_reverse,
    hr_deductions_run,
    hr_leave_balances,
)
from portal import portal_bp
from migrations.versions import h9i0j1k2l3m4_add_hr_deduction_details_and_reversal as deduction_migration
from migrations.versions import z4a5b6c7d8e9_add_leave_balance_renewal_policy as renewal_policy_migration
from migrations.versions import z5a6b7c8d9e0_add_leave_rollover_decisions as rollover_decision_migration


class MonthlyAttendanceDeductionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="monthly-deduction-test",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(cls.app)
        LoginManager(cls.app)
        cls.app.register_blueprint(portal_bp)
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

        self.user = User(
            email="deduction-employee@example.test",
            name="Deduction Employee",
            password_hash="not-used",
            role="EMPLOYEE",
        )
        self.schedule = WorkSchedule(
            name="Seven hours",
            kind="FIXED",
            start_time="08:00",
            end_time="15:00",
            is_active=True,
        )
        self.policy = WorkPolicy(
            name="Fixed hybrid",
            days_policy="HYBRID_WEEKLY_QUOTA",
            hybrid_office_days=3,
            hybrid_remote_days=2,
            hybrid_selection_mode="FIXED",
            hybrid_fixed_days_mask=7,
            location_policy="HYBRID",
            is_active=True,
        )
        db.session.add_all((self.user, self.schedule, self.policy))
        db.session.flush()
        self.employee_file = EmployeeFile(
            user_id=self.user.id,
            timeclock_code="D-100",
            hire_date="2026-03-01",
        )
        db.session.add_all((
            self.employee_file,
            WorkAssignment(
                name="Employee hybrid assignment",
                schedule_id=self.schedule.id,
                policy_id=self.policy.id,
                target_type="USER",
                target_user_id=self.user.id,
                is_active=True,
            ),
            SystemSetting(key="HR_WEEKLY_HOLIDAYS_MASK", value="48"),
            SystemSetting(key="HR_DEFAULT_SCHEDULE_ID", value=str(self.schedule.id)),
        ))
        db.session.commit()

    def _add_normal_office_summaries(self):
        skipped = {"2026-03-09", "2026-03-10", "2026-03-11"}
        current_day = date(2026, 3, 1)
        while current_day <= date(2026, 3, 31):
            day_str = current_day.isoformat()
            if current_day.weekday() in {0, 1, 2} and day_str not in skipped:
                db.session.add(AttendanceDailySummary(
                    user_id=self.user.id,
                    day=day_str,
                    first_in=datetime.combine(current_day, datetime.min.time()).replace(hour=8),
                    last_out=datetime.combine(current_day, datetime.min.time()).replace(hour=15),
                    late_minutes=30 if day_str == "2026-03-02" else 0,
                    early_leave_minutes=20 if day_str == "2026-03-03" else 0,
                    work_minutes=420,
                    status="OK",
                ))
            current_day += timedelta(days=1)

    def test_monthly_breakdown_applies_remote_mission_sick_and_special_exclusions(self):
        self._add_normal_office_summaries()
        sick_leave_type = HRLeaveType(
            code="SICK",
            name_ar="إجازة مرضية",
            deduct_from_balance=False,
            is_active=True,
        )
        personal_permission = HRPermissionType(
            code="PERSONAL",
            name_ar="مغادرة شخصية",
            counts_as_work=False,
            deduct_from_allowance=True,
        )
        sick_permission = HRPermissionType(
            code="SICK",
            name_ar="مغادرة مرضية",
            counts_as_work=False,
            deduct_from_allowance=True,
        )
        official_permission = HRPermissionType(
            code="OFFICIAL",
            name_ar="مغادرة رسمية",
            counts_as_work=True,
            deduct_from_allowance=True,
        )
        db.session.add_all((sick_leave_type, personal_permission, sick_permission, official_permission))
        db.session.flush()
        db.session.add_all((
            HROfficialMission(
                user_id=self.user.id,
                title="مهمة الوزارة",
                start_day="2026-03-09",
                end_day="2026-03-09",
            ),
            HRLeaveRequest(
                user_id=self.user.id,
                leave_type_id=sick_leave_type.id,
                start_date="2026-03-10",
                end_date="2026-03-10",
                status="APPROVED",
            ),
            HRPermissionRequest(
                user_id=self.user.id,
                permission_type_id=personal_permission.id,
                day="2026-03-02",
                from_time="10:00",
                to_time="12:00",
                status="APPROVED",
            ),
            HRPermissionRequest(
                user_id=self.user.id,
                permission_type_id=sick_permission.id,
                day="2026-03-03",
                from_time="10:00",
                to_time="12:00",
                status="APPROVED",
            ),
            HRPermissionRequest(
                user_id=self.user.id,
                permission_type_id=official_permission.id,
                day="2026-03-04",
                from_time="10:00",
                to_time="11:00",
                status="APPROVED",
            ),
            HRAttendanceSpecialCase(
                user_id=self.user.id,
                day="2026-03-03",
                day_to="2026-03-03",
                kind="EXCEPTION",
                field="EARLY_EXIT",
                applied=True,
                approval_status="APPROVED",
            ),
        ))
        db.session.commit()

        result = _monthly_attendance_deduction_breakdown(
            self.user.id,
            2026,
            3,
            minutes_per_day=420,
            allowance_minutes=60,
        )

        self.assertEqual(result["late_minutes"], 30)
        self.assertEqual(result["early_leave_minutes"], 0)
        self.assertEqual(result["absent_days"], 1)
        self.assertEqual(result["permission_minutes"], 120)
        self.assertEqual(result["chargeable_minutes"], 510)
        self.assertGreater(result["details"]["summary"]["remote_days"], 0)
        self.assertEqual(result["details"]["summary"]["mission_days"], 1)
        self.assertEqual(result["details"]["summary"]["approved_leave_days"], 1)

        departure_reasons = {
            row["permission_name"]: row["exclusion_code"]
            for row in result["details"]["departures"]
        }
        self.assertEqual(departure_reasons["مغادرة مرضية"], "EXEMPT_PERMISSION_TYPE")
        self.assertEqual(departure_reasons["مغادرة رسمية"], "OFFICIAL_DEPARTURE")

    def test_balance_correction_and_reversal_recalculate_remaining_balance(self):
        leave_type = HRLeaveType(
            code="ANNUAL",
            name_ar="إجازة سنوية",
            deduct_from_balance=True,
            is_active=True,
        )
        db.session.add(leave_type)
        db.session.flush()
        db.session.add(HRLeaveBalance(
            user_id=self.user.id,
            leave_type_id=leave_type.id,
            year=2026,
            total_days=10,
        ))
        db.session.add(HRLeaveBalanceAdjustment(
            user_id=self.user.id,
            leave_type_id=leave_type.id,
            year=2026,
            days_delta=2.0,
            reason="تصحيح قرار إداري",
            created_by_id=self.user.id,
        ))
        run = HRAttendanceDeductionRun(year=2026, month=3, status="FINAL")
        db.session.add(run)
        db.session.flush()
        db.session.add(HRAttendanceDeductionItem(
            run_id=run.id,
            user_id=self.user.id,
            deduction_leave_type_id=leave_type.id,
            leave_deduction_days=3.0,
        ))
        db.session.commit()

        entitlement = _leave_entitlement_days(self.user.id, leave_type, 2026)
        used = _leave_used_days_as_of(self.user.id, leave_type.id, 2026, date(2026, 12, 31))
        self.assertEqual(entitlement, 12.0)
        self.assertEqual(used, 3.0)
        self.assertEqual(entitlement - used, 9.0)

        run.status = "REVERSED"
        db.session.commit()
        used_after_reversal = _leave_used_days_as_of(
            self.user.id,
            leave_type.id,
            2026,
            date(2026, 12, 31),
        )
        self.assertEqual(used_after_reversal, 0.0)
        self.assertEqual(entitlement - used_after_reversal, 12.0)

    def test_reverse_action_requires_reason_and_restores_balance_effect(self):
        admin = User(
            email="deduction-admin@example.test",
            name="Deduction Admin",
            password_hash="not-used",
            role="SUPER_ADMIN",
        )
        leave_type = HRLeaveType(
            code="ANNUAL",
            name_ar="إجازة سنوية",
            deduct_from_balance=True,
            is_active=True,
        )
        db.session.add_all((admin, leave_type))
        db.session.flush()
        db.session.add(HRLeaveBalance(
            user_id=self.user.id,
            leave_type_id=leave_type.id,
            year=2026,
            total_days=10,
        ))
        run = HRAttendanceDeductionRun(year=2026, month=3, status="FINAL")
        db.session.add(run)
        db.session.flush()
        db.session.add(HRAttendanceDeductionItem(
            run_id=run.id,
            user_id=self.user.id,
            deduction_leave_type_id=leave_type.id,
            leave_deduction_days=2.0,
        ))
        db.session.commit()

        with self.app.test_request_context(
            f"/portal/hr/deductions/{run.id}/reverse",
            method="POST",
            data={"reversal_note": "إجازة معتمدة بأثر رجعي"},
        ):
            login_user(admin)
            response = hr_deduction_reverse(run.id)
            logout_user()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(run.status, "REVERSED")
        self.assertEqual(run.reversal_note, "إجازة معتمدة بأثر رجعي")
        self.assertEqual(
            _leave_used_days_as_of(self.user.id, leave_type.id, 2026, date(2026, 12, 31)),
            0.0,
        )

    def test_balance_correction_action_records_audited_adjustment(self):
        admin = User(
            email="balance-admin@example.test",
            name="Balance Admin",
            password_hash="not-used",
            role="SUPER_ADMIN",
        )
        leave_type = HRLeaveType(
            code="ANNUAL",
            name_ar="إجازة سنوية",
            deduct_from_balance=True,
            is_active=True,
        )
        db.session.add_all((admin, leave_type))
        db.session.flush()
        db.session.add(HRLeaveBalance(
            user_id=self.user.id,
            leave_type_id=leave_type.id,
            year=2026,
            total_days=10,
        ))
        db.session.commit()

        with self.app.test_request_context(
            f"/portal/hr/leaves/balances?user_id={self.user.id}&year=2026",
            method="POST",
            data={
                "action": "SET_ANNUAL_CURRENT_YEAR_LIMIT",
                "user_id": str(self.user.id),
                "year": "2026",
                "annual_current_year_limit": "30",
            },
        ):
            login_user(admin)
            response = hr_leave_balances()
            logout_user()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            SystemSetting.query.filter_by(key="HR_ANNUAL_LEAVE_CURRENT_YEAR_LIMIT").one().value,
            "30",
        )

        with self.app.test_request_context(
            f"/portal/hr/leaves/balances?user_id={self.user.id}&year=2026",
            method="POST",
            data={
                "action": "ADD_ADJUSTMENT",
                "user_id": str(self.user.id),
                "year": "2026",
                "leave_type_id": str(leave_type.id),
                "days_delta": "1.5",
                "reason": "تصحيح بعد اعتماد الخصم",
            },
        ):
            login_user(admin)
            response = hr_leave_balances()
            logout_user()

        adjustment = HRLeaveBalanceAdjustment.query.one()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(adjustment.days_delta, 1.5)
        self.assertEqual(adjustment.reason, "تصحيح بعد اعتماد الخصم")
        self.assertEqual(adjustment.created_by_id, admin.id)
        self.assertEqual(_leave_entitlement_days(self.user.id, leave_type, 2026), 11.5)

    def test_dedicated_permission_can_set_opening_leave_balance(self):
        manager = User(
            email="balance-manager@example.test",
            name="Balance Manager",
            password_hash="not-used",
            role="EMPLOYEE",
        )
        leave_type = HRLeaveType(
            code="ANNUAL",
            name_ar="إجازة سنوية",
            deduct_from_balance=True,
            is_active=True,
        )
        db.session.add_all((manager, leave_type))
        db.session.flush()
        db.session.add(UserPermission(
            user_id=manager.id,
            key=HR_LEAVE_BALANCES_MANAGE,
            is_allowed=True,
        ))
        db.session.commit()

        with self.app.test_request_context(
            f"/portal/hr/leaves/balances?user_id={self.user.id}&year=2026",
            method="POST",
            data={
                "user_id": str(self.user.id),
                "year": "2026",
                f"total_{leave_type.id}": "18",
            },
        ):
            login_user(manager)
            response = hr_leave_balances()
            logout_user()

        balance = HRLeaveBalance.query.one()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(balance.user_id, self.user.id)
        self.assertEqual(balance.leave_type_id, leave_type.id)
        self.assertEqual(balance.year, 2026)
        self.assertEqual(balance.total_days, 18)

    def test_compensatory_credit_is_audited_and_makes_balance_available(self):
        administrative_affairs = User(
            email="administrative-affairs@example.test",
            name="Administrative Affairs",
            password_hash="not-used",
            role="HR_MANAGER",
        )
        db.session.add(administrative_affairs)
        db.session.flush()
        db.session.add(UserPermission(
            user_id=administrative_affairs.id,
            key=HR_LEAVE_BALANCES_MANAGE,
            is_allowed=True,
        ))
        db.session.commit()

        with self.app.test_request_context(
            f"/portal/hr/leaves/balances?user_id={self.user.id}&year=2026",
            method="POST",
            data={
                "action": "ADD_COMPENSATORY_CREDIT",
                "user_id": str(self.user.id),
                "year": "2026",
                "days_delta": "2.5",
                "reason": "عمل إضافي معتمد",
            },
        ):
            login_user(administrative_affairs)
            response = hr_leave_balances()
            logout_user()

        compensatory_type = HRLeaveType.query.filter_by(code="COMPENSATORY").one()
        adjustment = HRLeaveBalanceAdjustment.query.filter_by(
            user_id=self.user.id,
            leave_type_id=compensatory_type.id,
            year=2026,
        ).one()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(adjustment.days_delta, 2.5)
        self.assertEqual(adjustment.created_by_id, administrative_affairs.id)
        self.assertIn("إضافة رصيد تعويضي", adjustment.reason)
        self.assertEqual(_leave_entitlement_days(self.user.id, compensatory_type, 2026), 2.5)
        self.assertIsNone(_compensatory_leave_balance_error(
            self.user.id,
            compensatory_type,
            date(2026, 3, 1),
            date(2026, 3, 2),
        ))
        self.assertIn("غير كافٍ", _compensatory_leave_balance_error(
            self.user.id,
            compensatory_type,
            date(2026, 3, 1),
            date(2026, 3, 4),
        ) or "")

    def test_report_view_permission_cannot_change_opening_leave_balance(self):
        report_viewer = User(
            email="balance-viewer@example.test",
            name="Balance Viewer",
            password_hash="not-used",
            role="EMPLOYEE",
        )
        leave_type = HRLeaveType(
            code="ANNUAL",
            name_ar="إجازة سنوية",
            deduct_from_balance=True,
            is_active=True,
        )
        db.session.add_all((report_viewer, leave_type))
        db.session.flush()
        db.session.add(UserPermission(
            user_id=report_viewer.id,
            key=HR_REPORTS_VIEW,
            is_allowed=True,
        ))
        db.session.commit()

        with self.app.test_request_context(
            f"/portal/hr/leaves/balances?user_id={self.user.id}&year=2026",
            method="POST",
            data={
                "user_id": str(self.user.id),
                "year": "2026",
                f"total_{leave_type.id}": "18",
            },
        ):
            login_user(report_viewer)
            with self.assertRaises(Forbidden):
                hr_leave_balances()
            logout_user()

        self.assertEqual(HRLeaveBalance.query.count(), 0)

    def test_all_filtered_action_creates_items_for_every_matching_employee(self):
        admin = User(
            email="run-admin@example.test",
            name="Run Admin",
            password_hash="not-used",
            role="SUPER_ADMIN",
        )
        second_user = User(
            email="second-employee@example.test",
            name="Second Employee",
            password_hash="not-used",
            role="EMPLOYEE",
        )
        db.session.add_all((admin, second_user))
        db.session.flush()
        db.session.add(EmployeeFile(
            user_id=second_user.id,
            timeclock_code="D-200",
            hire_date="2026-03-01",
        ))
        db.session.commit()

        with self.app.test_request_context(
            "/portal/hr/deductions/run",
            method="POST",
            data={
                "year": "2099",
                "month": "1",
                "selection_scope": "ALL_FILTERED",
            },
        ):
            login_user(admin)
            response = hr_deductions_run()
            logout_user()

        run = HRAttendanceDeductionRun.query.one()
        self.assertEqual(response.status_code, 302)
        self.assertEqual({item.user_id for item in run.items}, {self.user.id, second_user.id})

    def test_approval_recalculates_when_leave_was_approved_after_preview(self):
        self._add_normal_office_summaries()
        for day_value in (date(2026, 3, 9), date(2026, 3, 10)):
            db.session.add(AttendanceDailySummary(
                user_id=self.user.id,
                day=day_value.isoformat(),
                first_in=datetime.combine(day_value, datetime.min.time()).replace(hour=8),
                last_out=datetime.combine(day_value, datetime.min.time()).replace(hour=15),
                work_minutes=420,
                status="OK",
            ))
        leave_type = HRLeaveType(
            code="SICK",
            name_ar="إجازة مرضية",
            deduct_from_balance=False,
            is_active=True,
        )
        admin = User(
            email="approval-admin@example.test",
            name="Approval Admin",
            password_hash="not-used",
            role="SUPER_ADMIN",
        )
        db.session.add_all((leave_type, admin))
        db.session.commit()

        preview = _monthly_attendance_deduction_breakdown(
            self.user.id,
            2026,
            3,
            minutes_per_day=420,
            allowance_minutes=60,
        )
        self.assertEqual(preview["absent_days"], 1)
        run = HRAttendanceDeductionRun(
            year=2026,
            month=3,
            status="DRAFT",
            config_snapshot_json=json.dumps({
                "hours_per_day": 7,
                "deduction_sequence": "SALARY_ONLY",
            }),
        )
        db.session.add(run)
        db.session.flush()
        item = HRAttendanceDeductionItem(
            run_id=run.id,
            user_id=self.user.id,
            absent_days=preview["absent_days"],
            permission_allowance_minutes=60,
            chargeable_minutes=preview["chargeable_minutes"],
            salary_deduction_days=1.0,
            amount=1.0,
            details_json=json.dumps(preview["details"], ensure_ascii=False),
        )
        db.session.add_all((
            item,
            HRLeaveRequest(
                user_id=self.user.id,
                leave_type_id=leave_type.id,
                start_date="2026-03-11",
                end_date="2026-03-11",
                status="APPROVED",
            ),
        ))
        db.session.commit()

        with self.app.test_request_context(
            f"/portal/hr/deductions/{run.id}/approve",
            method="POST",
        ):
            login_user(admin)
            first_response = hr_deduction_approve(run.id)
            logout_user()
        self.assertEqual(first_response.status_code, 302)
        self.assertEqual(run.status, "DRAFT")
        self.assertEqual(item.absent_days, 0)
        self.assertEqual(item.chargeable_minutes, 50)

        with self.app.test_request_context(
            f"/portal/hr/deductions/{run.id}/approve",
            method="POST",
        ):
            login_user(admin)
            second_response = hr_deduction_approve(run.id)
            logout_user()
        self.assertEqual(second_response.status_code, 302)
        self.assertEqual(run.status, "FINAL")


    def test_annual_balance_splits_2026_then_decides_2025_in_2027(self):
        admin = User(
            email="annual-rollover-admin@example.test",
            name="Annual Rollover Admin",
            password_hash="not-used",
            role="EMPLOYEE",
        )
        leave_type = HRLeaveType(
            code="ANNUAL",
            name_ar="Annual leave",
            deduct_from_balance=True,
            default_balance_days=0,
            is_active=True,
        )
        db.session.add_all((admin, leave_type))
        db.session.flush()
        db.session.add_all((
            UserPermission(user_id=admin.id, key=HR_LEAVE_BALANCES_MANAGE, is_allowed=True),
            HRLeaveBalance(user_id=self.user.id, leave_type_id=leave_type.id, year=2026, total_days=45),
            HRLeaveBalance(user_id=self.user.id, leave_type_id=leave_type.id, year=2027, total_days=20),
        ))
        db.session.commit()

        with self.app.test_request_context(
            f"/portal/hr/leaves/balances?user_id={self.user.id}&year=2026",
            method="POST",
            data={
                "action": "PREPARE_ANNUAL_BALANCE_BUCKETS",
                "user_id": str(self.user.id),
                "year": "2026",
            },
        ):
            login_user(admin)
            response = hr_leave_balances()
            logout_user()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(_leave_entitlement_days(self.user.id, leave_type, 2026), 30)
        self.assertEqual(_leave_entitlement_days(self.user.id, leave_type, 2025), 15)

        bucket_row = _annual_bucket_rows(self.user.id, 2026, [leave_type])[0]
        self.assertEqual(bucket_row["current_remaining"], 30)
        self.assertEqual(bucket_row["previous_year"], 2025)
        self.assertEqual(bucket_row["previous_remaining"], 15)

        split_adjustments = HRLeaveBalanceAdjustment.query.filter_by(
            user_id=self.user.id,
            leave_type_id=leave_type.id,
        ).order_by(HRLeaveBalanceAdjustment.year.asc()).all()
        self.assertEqual([(row.year, row.days_delta) for row in split_adjustments], [
            (2025, 15),
            (2026, -15),
        ])

        decision, became_active, correction, error = _save_leave_rollover_decision(
            self.user.id,
            leave_type,
            2025,
            2027,
            decision_code="TRANSFER",
            transfer_days=7,
            note="approved exception",
            actor_id=admin.id,
            effective_day=date(2026, 12, 31),
        )
        self.assertIsNone(error)
        self.assertFalse(became_active)
        self.assertEqual(correction, 0)
        self.assertIsNotNone(decision)
        db.session.commit()

        saved_decision = HRLeaveRolloverDecision.query.one()
        self.assertEqual(saved_decision.decision, "TRANSFER")
        self.assertEqual(saved_decision.transfer_days, 7)
        self.assertIsNone(saved_decision.applied_at)

        # Saving early is purely a decision: it must not affect either year's
        # balance before the first day of its target year.
        self.assertEqual(_leave_entitlement_days(self.user.id, leave_type, 2025), 15)
        self.assertEqual(_leave_entitlement_days(self.user.id, leave_type, 2026), 30)
        self.assertEqual(_leave_entitlement_days(self.user.id, leave_type, 2027), 20)

        planned_rows = _leave_rollover_rows(
            self.user.id,
            2027,
            [leave_type],
            as_of=date(2026, 12, 31),
        )
        planned_row = next(row for row in planned_rows if row["source_year"] == 2025)
        self.assertEqual(planned_row["source_year"], 2025)
        self.assertEqual(planned_row["remaining"], 15)
        self.assertEqual(planned_row["decision"], "TRANSFER")
        self.assertEqual(planned_row["transfer_days"], 7)
        self.assertTrue(planned_row["is_planned"])
        self.assertFalse(planned_row["is_active"])

        activated = _activate_due_leave_rollover_decisions(
            effective_day=date(2027, 1, 1),
        )
        self.assertEqual([item.id for item in activated], [saved_decision.id])
        db.session.commit()
        db.session.refresh(saved_decision)

        self.assertEqual(_leave_entitlement_days(self.user.id, leave_type, 2025), 0)
        self.assertEqual(_leave_entitlement_days(self.user.id, leave_type, 2026), 30)
        self.assertEqual(_leave_entitlement_days(self.user.id, leave_type, 2027), 27)

        active_rows = _leave_rollover_rows(
            self.user.id,
            2027,
            [leave_type],
            as_of=date(2027, 1, 1),
        )
        active_row = next(row for row in active_rows if row["source_year"] == 2025)
        self.assertTrue(active_row["is_active"])
        self.assertEqual(active_row["applied_transfer_days"], 7)
        self.assertEqual(active_row["deleted_days"], 8)

        # A later authorised change does not rewrite the activation entries;
        # it appends a correction to the target-year balance.
        revised, became_active, correction, error = _save_leave_rollover_decision(
            self.user.id,
            leave_type,
            2025,
            2027,
            decision_code="TRANSFER",
            transfer_days=10,
            note="expanded exception",
            actor_id=admin.id,
            effective_day=date(2027, 1, 2),
        )
        self.assertIsNone(error)
        self.assertTrue(became_active)
        self.assertEqual(correction, 3)
        self.assertEqual(revised.applied_transfer_days, 10)
        db.session.commit()
        self.assertEqual(_leave_entitlement_days(self.user.id, leave_type, 2027), 30)
        self.assertTrue(HRLeaveBalanceAdjustment.query.filter(
            HRLeaveBalanceAdjustment.reason.contains("DECISION_REVISION=1")
        ).count())

        next_year_rows = _leave_rollover_rows(self.user.id, 2028, [leave_type])
        self.assertEqual(len(next_year_rows), 2)
        next_year_current = next(
            row for row in next_year_rows if row["source_year"] == 2027
        )
        next_year_expired = next(
            row for row in next_year_rows if row["source_year"] == 2026
        )
        self.assertEqual(next_year_current["remaining"], 30)
        self.assertEqual(next_year_expired["remaining"], 30)
        self.assertTrue(next_year_current["can_decide"])

        # Replaying the activation job is safe and never duplicates the
        # transfer entries.
        self.assertEqual(
            _activate_due_leave_rollover_decisions(effective_day=date(2027, 1, 3)),
            [],
        )
        self.assertEqual(HRLeaveBalanceAdjustment.query.filter_by(
            user_id=self.user.id,
            leave_type_id=leave_type.id,
        ).count(), 5)

    def test_annual_48_day_balance_consumes_old_bucket_before_2027_deletion(self):
        """A 2026 annual balance of 48 remains one declining balance.

        The screen must expose 30 days as the 2026 bucket and 18 as the 2025
        bucket.  Leave taken in 2026 uses the older 18 days first, and the
        balance still left in that old bucket is the only amount deleted in
        2027.
        """
        admin = User(
            email="annual-48-admin@example.test",
            name="Annual 48 Admin",
            password_hash="not-used",
            role="EMPLOYEE",
        )
        leave_type = HRLeaveType(
            code="ANNUAL",
            name_ar="Annual leave",
            deduct_from_balance=True,
            default_balance_days=0,
            day_count_basis="CALENDAR_DAYS",
            is_active=True,
        )
        db.session.add_all((admin, leave_type))
        db.session.flush()
        db.session.add_all((
            UserPermission(user_id=admin.id, key=HR_LEAVE_BALANCES_MANAGE, is_allowed=True),
            HRLeaveBalance(
                user_id=self.user.id,
                leave_type_id=leave_type.id,
                year=2026,
                total_days=30,
            ),
            HRLeaveBalanceAdjustment(
                user_id=self.user.id,
                leave_type_id=leave_type.id,
                year=2026,
                days_delta=18,
                reason="رصيد مرحل مثبت قبل التقسيم",
                created_by_id=admin.id,
            ),
        ))
        db.session.commit()

        with self.app.test_request_context(
            f"/portal/hr/leaves/balances?user_id={self.user.id}&year=2026",
            method="POST",
            data={
                "action": "PREPARE_ANNUAL_BALANCE_BUCKETS",
                "user_id": str(self.user.id),
                "year": "2026",
            },
        ):
            login_user(admin)
            response = hr_leave_balances()
            logout_user()
        self.assertEqual(response.status_code, 302)

        # 30 + 18 is the same actual 48-day balance shown before splitting.
        self.assertEqual(_leave_entitlement_days(self.user.id, leave_type, 2026), 30)
        self.assertEqual(_leave_entitlement_days(self.user.id, leave_type, 2025), 18)
        before_leave = _annual_bucket_rows(self.user.id, 2026, [leave_type])[0]
        self.assertEqual(before_leave["current_remaining"], 30)
        self.assertEqual(before_leave["previous_remaining"], 18)

        # A five-day annual leave in 2026 uses the older bucket first.  The
        # employee's total therefore changes from 48 to 43, not from 48 to
        # 48 with an isolated 2026 balance.
        db.session.add(HRLeaveRequest(
            user_id=self.user.id,
            leave_type_id=leave_type.id,
            start_date="2026-02-01",
            end_date="2026-02-05",
            status="APPROVED",
        ))
        db.session.commit()
        usage = _leave_balance_usage_by_year(
            self.user.id,
            leave_type,
            date(2026, 12, 31),
        )
        self.assertEqual(usage[2025], 5)
        self.assertEqual(usage.get(2026, 0), 0)

        after_leave = _annual_bucket_rows(self.user.id, 2026, [leave_type])[0]
        self.assertEqual(after_leave["current_remaining"], 30)
        self.assertEqual(after_leave["previous_remaining"], 13)
        self.assertEqual(
            after_leave["current_remaining"] + after_leave["previous_remaining"],
            43,
        )
        displayed = _leave_balance_display_values(self.user.id, leave_type, 2026)
        self.assertEqual(displayed["total"], 48)
        self.assertEqual(displayed["used"], 5)
        self.assertEqual(displayed["remaining"], 43)

        # The older 2025 bucket cannot be forged into a transfer decision.
        with self.app.test_request_context(
            f"/portal/hr/leaves/balances?user_id={self.user.id}&year=2027",
            method="POST",
            data={
                "action": "SAVE_ROLLOVER_DECISION",
                "user_id": str(self.user.id),
                "year": "2027",
                f"rollover_2026_{leave_type.id}": "TRANSFER",
                f"rollover_transfer_days_2026_{leave_type.id}": "30",
                f"rollover_2025_{leave_type.id}": "TRANSFER",
                f"rollover_transfer_days_2025_{leave_type.id}": "13",
            },
        ):
            login_user(admin)
            rejected_response = hr_leave_balances()
            logout_user()
        self.assertEqual(rejected_response.status_code, 302)
        self.assertEqual(HRLeaveRolloverDecision.query.count(), 0)

        # Saving the 2027 screen creates two policy-controlled decisions:
        # transfer only the 2026 bucket (30) and delete the older 2025
        # bucket (13 after the five-day leave).
        with self.app.test_request_context(
            f"/portal/hr/leaves/balances?user_id={self.user.id}&year=2027",
            method="POST",
            data={
                "action": "SAVE_ROLLOVER_DECISION",
                "user_id": str(self.user.id),
                "year": "2027",
                f"rollover_2026_{leave_type.id}": "TRANSFER",
                f"rollover_transfer_days_2026_{leave_type.id}": "30",
                f"rollover_2025_{leave_type.id}": "DELETE",
                f"rollover_reason_2025_{leave_type.id}": "حذف الرصيد الأقدم",
            },
        ):
            login_user(admin)
            response = hr_leave_balances()
            logout_user()
        self.assertEqual(response.status_code, 302)

        decisions = {
            row.source_year: row
            for row in HRLeaveRolloverDecision.query.order_by(
                HRLeaveRolloverDecision.source_year.asc()
            ).all()
        }
        self.assertEqual(decisions[2026].decision, "TRANSFER")
        self.assertEqual(decisions[2026].transfer_days, 30)
        self.assertEqual(decisions[2025].decision, "DELETE")
        self.assertEqual(decisions[2025].transfer_days, 0)

        planned_rows = _leave_rollover_rows(
            self.user.id,
            2027,
            [leave_type],
            as_of=date(2026, 12, 31),
        )
        current_row = next(row for row in planned_rows if row["source_year"] == 2026)
        expired_row = next(row for row in planned_rows if row["source_year"] == 2025)
        self.assertEqual(current_row["remaining"], 30)
        self.assertEqual(current_row["required_decision"], "TRANSFER")
        self.assertEqual(current_row["suggested_transfer_days"], 30)
        self.assertTrue(current_row["is_planned"])
        self.assertEqual(expired_row["remaining"], 13)
        self.assertEqual(expired_row["required_decision"], "DELETE")
        self.assertTrue(expired_row["is_planned"])

        activated = _activate_due_leave_rollover_decisions(
            effective_day=date(2027, 1, 1),
        )
        self.assertEqual(
            {item.source_year for item in activated},
            {2025, 2026},
        )
        db.session.commit()

        active_rows = _leave_rollover_rows(
            self.user.id,
            2027,
            [leave_type],
            as_of=date(2027, 1, 1),
        )
        active_current = next(row for row in active_rows if row["source_year"] == 2026)
        active_expired = next(row for row in active_rows if row["source_year"] == 2025)
        self.assertTrue(active_current["is_active"])
        self.assertEqual(active_current["applied_transfer_days"], 30)
        self.assertTrue(active_expired["is_active"])
        self.assertEqual(active_expired["deleted_days"], 13)
        self.assertEqual(_leave_entitlement_days(self.user.id, leave_type, 2027), 30)

    def test_cross_year_leave_uses_start_year_balance_before_new_year_balance(self):
        sick_type = HRLeaveType(
            code="SICK",
            name_ar="Sick leave",
            deduct_from_balance=True,
            default_balance_days=10,
            balance_renewal_policy="YEARLY",
            day_count_basis="CALENDAR_DAYS",
            is_active=True,
        )
        db.session.add(sick_type)
        db.session.flush()
        db.session.add_all((
            HRLeaveBalance(user_id=self.user.id, leave_type_id=sick_type.id, year=2026, total_days=2),
            HRLeaveBalance(user_id=self.user.id, leave_type_id=sick_type.id, year=2027, total_days=10),
            HRLeaveRequest(
                user_id=self.user.id,
                leave_type_id=sick_type.id,
                start_date="2026-12-30",
                end_date="2027-01-03",
                status="APPROVED",
            ),
        ))
        db.session.commit()

        usage = _leave_balance_usage_by_year(
            self.user.id,
            sick_type,
            date(2027, 12, 31),
        )
        self.assertEqual(usage[2026], 2)
        self.assertEqual(usage[2027], 3)
        self.assertEqual(
            _leave_used_days_as_of(self.user.id, sick_type.id, 2026, date(2026, 12, 31)),
            2,
        )
        self.assertEqual(
            _leave_used_days_as_of(self.user.id, sick_type.id, 2027, date(2027, 12, 31)),
            3,
        )
        self.assertEqual(_leave_entitlement_days(self.user.id, sick_type, 2028), 10)

    def test_compensatory_balance_is_manual_but_can_fund_a_cross_year_request(self):
        compensatory_type = HRLeaveType(
            code="COMPENSATORY",
            name_ar="Compensatory leave",
            deduct_from_balance=True,
            default_balance_days=20,
            balance_renewal_policy="MANUAL",
            day_count_basis="CALENDAR_DAYS",
            is_active=True,
        )
        db.session.add(compensatory_type)
        db.session.flush()
        db.session.add_all((
            HRLeaveBalance(user_id=self.user.id, leave_type_id=compensatory_type.id, year=2026, total_days=2),
            HRLeaveBalance(user_id=self.user.id, leave_type_id=compensatory_type.id, year=2027, total_days=3),
        ))
        db.session.commit()

        self.assertEqual(_leave_entitlement_days(self.user.id, compensatory_type, 2028), 0)
        self.assertIsNone(_compensatory_leave_balance_error(
            self.user.id,
            compensatory_type,
            date(2026, 12, 30),
            date(2027, 1, 3),
        ))
        self.assertIsNotNone(_compensatory_leave_balance_error(
            self.user.id,
            compensatory_type,
            date(2026, 12, 29),
            date(2027, 1, 3),
        ))

    def test_hajj_leave_is_rejected_after_an_approved_previous_request(self):
        hajj_type = HRLeaveType(
            code="H",
            name_ar="Hajj leave",
            deduct_from_balance=False,
            balance_renewal_policy="ONCE",
            is_active=True,
        )
        db.session.add(hajj_type)
        db.session.flush()
        db.session.add(HRLeaveRequest(
            user_id=self.user.id,
            leave_type_id=hajj_type.id,
            start_date="2024-06-10",
            end_date="2024-06-15",
            status="APPROVED",
        ))
        db.session.commit()

        self.assertIsNotNone(_one_time_leave_error(self.user.id, hajj_type))

    @unittest.skip("Superseded by the annual-bucket rollover policy test above.")
    def test_leave_rollover_records_transfer_and_no_transfer_once(self):
        admin = User(
            email="rollover-admin@example.test",
            name="Rollover Admin",
            password_hash="not-used",
            role="EMPLOYEE",
        )
        leave_type = HRLeaveType(
            code="ANNUAL",
            name_ar="إجازة سنوية",
            deduct_from_balance=True,
            is_active=True,
        )
        db.session.add_all((admin, leave_type))
        db.session.flush()
        db.session.add_all((
            UserPermission(user_id=admin.id, key=HR_LEAVE_BALANCES_MANAGE, is_allowed=True),
            HRLeaveBalance(user_id=self.user.id, leave_type_id=leave_type.id, year=2025, total_days=17),
            HRLeaveBalance(user_id=self.user.id, leave_type_id=leave_type.id, year=2024, total_days=22),
        ))
        db.session.commit()

        post_data = {
            "action": "ROLLOVER",
            "user_id": str(self.user.id),
            "year": "2026",
            f"rollover_2025_{leave_type.id}": "TRANSFER",
            f"rollover_reason_2025_{leave_type.id}": "تعذر منح الموظف إجازته بسبب حاجة العمل",
            f"rollover_2024_{leave_type.id}": "KEEP",
        }
        with self.app.test_request_context(
            f"/portal/hr/leaves/balances?user_id={self.user.id}&year=2026",
            method="POST",
            data=post_data,
        ):
            login_user(admin)
            response = hr_leave_balances()
            logout_user()
        self.assertEqual(response.status_code, 302)

        source_adjustment = HRLeaveBalanceAdjustment.query.filter_by(
            user_id=self.user.id, leave_type_id=leave_type.id, year=2025
        ).one()
        self.assertEqual(source_adjustment.days_delta, -17)
        self.assertIn("DECISION=TRANSFER", source_adjustment.reason)
        target_adjustments = HRLeaveBalanceAdjustment.query.filter_by(
            user_id=self.user.id, leave_type_id=leave_type.id, year=2026
        ).all()
        self.assertEqual(sorted(row.days_delta for row in target_adjustments), [0, 17])
        self.assertTrue(any("DECISION=KEEP" in row.reason for row in target_adjustments))
        rollover_rows = _leave_rollover_rows(self.user.id, 2026, [leave_type])
        transferred_row = next(row for row in rollover_rows if row["source_year"] == 2025)
        self.assertEqual(transferred_row["total"], 17)
        self.assertEqual(transferred_row["remaining"], 17)
        self.assertEqual(transferred_row["decision"], "TRANSFER")

        with self.app.test_request_context(
            f"/portal/hr/leaves/balances?user_id={self.user.id}&year=2026",
            method="POST",
            data=post_data,
        ):
            login_user(admin)
            hr_leave_balances()
            logout_user()
        self.assertEqual(HRLeaveBalanceAdjustment.query.count(), 3)


class MonthlyDeductionMigrationTests(unittest.TestCase):
    def test_upgrade_is_safe_after_runtime_schema_creation(self):
        engine = sa.create_engine("sqlite:///:memory:")
        metadata = sa.MetaData()
        sa.Table("users", metadata, sa.Column("id", sa.Integer, primary_key=True))
        sa.Table("hr_leave_type", metadata, sa.Column("id", sa.Integer, primary_key=True))
        sa.Table(
            "hr_permission_type",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("code", sa.String(50)),
            sa.Column("name_ar", sa.String(200)),
            sa.Column("deduct_from_allowance", sa.Boolean, nullable=False, default=True),
        )
        sa.Table(
            "hr_att_deduction_run",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("reversed_at", sa.DateTime),
            sa.Column("reversed_by_id", sa.Integer, sa.ForeignKey("users.id")),
            sa.Column("reversal_note", sa.Text),
        )
        sa.Table(
            "hr_att_deduction_item",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("details_json", sa.Text),
        )
        sa.Table(
            "hr_leave_balance_adjustment",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), index=True),
            sa.Column("leave_type_id", sa.Integer, sa.ForeignKey("hr_leave_type.id"), index=True),
            sa.Column("year", sa.Integer, index=True),
            sa.Column("days_delta", sa.Float, nullable=False),
            sa.Column("reason", sa.Text, nullable=False),
            sa.Column("created_at", sa.DateTime, nullable=False, index=True),
            sa.Column("created_by_id", sa.Integer, sa.ForeignKey("users.id"), index=True),
            sa.Index(
                "ix_hr_leave_balance_adjustment_user_year_type",
                "user_id",
                "year",
                "leave_type_id",
            ),
        )
        metadata.create_all(engine)

        with engine.begin() as connection:
            operations = Operations(MigrationContext.configure(connection))
            original_operations = deduction_migration.op
            deduction_migration.op = operations
            try:
                deduction_migration.upgrade()
                deduction_migration.upgrade()
            finally:
                deduction_migration.op = original_operations

            inspector = sa.inspect(connection)
            self.assertIn("details_json", {
                column["name"] for column in inspector.get_columns("hr_att_deduction_item")
            })
            self.assertIn("reversal_note", {
                column["name"] for column in inspector.get_columns("hr_att_deduction_run")
            })
            self.assertIn("hr_leave_balance_adjustment", inspector.get_table_names())


class LeaveBalanceRenewalPolicyMigrationTests(unittest.TestCase):
    def test_upgrade_adds_policy_and_normalizes_configured_leave_types(self):
        engine = sa.create_engine("sqlite:///:memory:")
        metadata = sa.MetaData()
        leave_type = sa.Table(
            "hr_leave_type",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("code", sa.String(50)),
            sa.Column("deduct_from_balance", sa.Boolean, nullable=False, default=False),
        )
        metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(leave_type.insert(), [
                {"code": "SICK", "deduct_from_balance": False},
                {"code": "M", "deduct_from_balance": False},
                {"code": "Y", "deduct_from_balance": False},
                {"code": "COMPENSATORY", "deduct_from_balance": True},
                {"code": "H", "deduct_from_balance": False},
            ])
            operations = Operations(MigrationContext.configure(connection))
            original_operations = renewal_policy_migration.op
            renewal_policy_migration.op = operations
            try:
                renewal_policy_migration.upgrade()
                renewal_policy_migration.upgrade()
            finally:
                renewal_policy_migration.op = original_operations

            inspector = sa.inspect(connection)
            self.assertIn("balance_renewal_policy", {
                column["name"] for column in inspector.get_columns("hr_leave_type")
            })
            rows = connection.execute(sa.text(
                "SELECT code, deduct_from_balance, balance_renewal_policy "
                "FROM hr_leave_type ORDER BY id"
            )).mappings().all()
            self.assertEqual(rows[0]["balance_renewal_policy"], "YEARLY")
            self.assertTrue(rows[0]["deduct_from_balance"])
            self.assertEqual(rows[1]["balance_renewal_policy"], "YEARLY")
            self.assertTrue(rows[1]["deduct_from_balance"])
            self.assertEqual(rows[2]["balance_renewal_policy"], "YEARLY")
            self.assertTrue(rows[2]["deduct_from_balance"])
            self.assertEqual(rows[3]["balance_renewal_policy"], "MANUAL")
            self.assertEqual(rows[4]["balance_renewal_policy"], "ONCE")


class LeaveRolloverDecisionMigrationTests(unittest.TestCase):
    def test_upgrade_creates_planned_rollover_decision_table(self):
        engine = sa.create_engine("sqlite:///:memory:")
        metadata = sa.MetaData()
        sa.Table("users", metadata, sa.Column("id", sa.Integer, primary_key=True))
        sa.Table("hr_leave_type", metadata, sa.Column("id", sa.Integer, primary_key=True))
        metadata.create_all(engine)

        with engine.begin() as connection:
            operations = Operations(MigrationContext.configure(connection))
            original_operations = rollover_decision_migration.op
            rollover_decision_migration.op = operations
            try:
                # It may run after a runtime schema upgrade, so it is safe to
                # invoke more than once.
                rollover_decision_migration.upgrade()
                rollover_decision_migration.upgrade()
            finally:
                rollover_decision_migration.op = original_operations

            inspector = sa.inspect(connection)
            self.assertIn("hr_leave_rollover_decision", inspector.get_table_names())
            columns = {
                column["name"]
                for column in inspector.get_columns("hr_leave_rollover_decision")
            }
            self.assertTrue({
                "source_year",
                "target_year",
                "decision",
                "transfer_days",
                "applied_at",
                "created_by_id",
                "updated_by_id",
            }.issubset(columns))


if __name__ == "__main__":
    unittest.main()
