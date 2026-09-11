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
    _leave_entitlement_days,
    _leave_used_days_as_of,
    _monthly_attendance_deduction_breakdown,
    hr_deduction_approve,
    hr_deduction_reverse,
    hr_deductions_run,
    hr_leave_balances,
)
from portal import portal_bp
from migrations.versions import h9i0j1k2l3m4_add_hr_deduction_details_and_reversal as deduction_migration


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


if __name__ == "__main__":
    unittest.main()
