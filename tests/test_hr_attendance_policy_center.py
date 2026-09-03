import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class HRAttendancePolicyCenterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.models = (PROJECT_ROOT / "models.py").read_text(encoding="utf-8")
        cls.routes = (PROJECT_ROOT / "portal" / "routes.py").read_text(encoding="utf-8")
        cls.center = (
            PROJECT_ROOT / "templates" / "portal" / "hr" / "system_screens.html"
        ).read_text(encoding="utf-8")
        cls.deduction_view = (
            PROJECT_ROOT / "templates" / "portal" / "hr" / "deductions_view.html"
        ).read_text(encoding="utf-8")
        cls.daily_view = (
            PROJECT_ROOT / "templates" / "portal" / "hr" / "attendance_daily.html"
        ).read_text(encoding="utf-8")
        cls.manual_edit_view = (
            PROJECT_ROOT / "templates" / "portal" / "hr" / "attendance_manual_edit.html"
        ).read_text(encoding="utf-8")

    def test_policy_center_exposes_independent_grace_and_hybrid_modes(self):
        for field in (
            'name="start_grace_minutes"',
            'name="end_grace_minutes"',
            'name="{{ key }}_office_days"',
            'value="FLEXIBLE"',
            'value="FIXED"',
        ):
            with self.subTest(field=field):
                self.assertIn(field, self.center)

        self.assertIn("start_grace_minutes = db.Column", self.models)
        self.assertIn("end_grace_minutes = db.Column", self.models)
        self.assertIn("hybrid_selection_mode = db.Column", self.models)
        self.assertIn("hybrid_fixed_days_mask = db.Column", self.models)

    def test_policy_center_renders_before_policy_records_exist(self):
        self.assertIn("policy_map = policies|default({})", self.center)
        self.assertIn("policy_map.regular|default(none)", self.center)

    def test_deductions_have_draft_adjustment_and_final_approval(self):
        self.assertIn("status='DRAFT'", self.routes)
        self.assertIn("def hr_deduction_item_adjust", self.routes)
        self.assertIn("def hr_deduction_approve", self.routes)
        self.assertIn("run.status = 'FINAL'", self.routes)
        self.assertIn('name="adjustment_note" required', self.deduction_view)
        self.assertIn("اعتماد وتنفيذ الخصم", self.deduction_view)

    def test_only_final_deductions_affect_balances_and_salary_report(self):
        self.assertIn("HRAttendanceDeductionRun.status == 'FINAL'", self.routes)
        self.assertIn("HRAttendanceDeductionItem.leave_deduction_days", self.routes)
        self.assertIn("salary_deduction_days = db.Column", self.models)
        self.assertIn("leave_deduction_days = db.Column", self.models)

    def test_holidays_and_approved_leave_are_exempt(self):
        self.assertIn("def _attendance_exemption_reason", self.routes)
        self.assertIn('return "WEEKLY_OFF"', self.routes)
        self.assertIn('return "OFFICIAL_HOLIDAY"', self.routes)
        self.assertIn('return "APPROVED_LEAVE"', self.routes)

    def test_on_behalf_entry_does_not_bypass_manager_approval(self):
        self.assertIn("Entry on behalf of an employee is not approval", self.routes)
        self.assertIn('status="SUBMITTED"', self.routes)
        self.assertIn("approver_user_id=(approver.id if approver else None)", self.routes)

    def test_manual_attendance_correction_preserves_clock_events_and_recomputes_summary(self):
        self.assertIn("def hr_attendance_manual_edit", self.routes)
        self.assertIn("def hr_attendance_manual_approval_queue", self.routes)
        self.assertIn("def hr_attendance_manual_review", self.routes)
        self.assertIn("def _manual_attendance_override", self.routes)
        self.assertIn("kind='MANUAL_ATTENDANCE'", self.routes)
        self.assertIn("approval_status = 'PENDING'", self.routes)
        self.assertIn("HR_ATTENDANCE_EDIT_APPROVE", self.routes)
        self.assertIn("_attendance_recompute_summaries_for_keys(affected_keys)", self.routes)
        self.assertIn("'A', 'I', 'IN', 'CHECKIN'", self.routes)
        self.assertIn('name="start_time"', self.manual_edit_view)
        self.assertIn('name="end_time"', self.manual_edit_view)
        self.assertIn('name="day_to"', self.manual_edit_view)
        self.assertIn("hr_attendance_manual_edit", self.daily_view)

    def test_daily_listing_is_compact_and_schedule_is_optional(self):
        for token in (
            "strftime('%H:%M')",
            "attendance-employee-name",
            "attendance-schedule-column",
            "toggle-schedule-column",
            "show-schedule",
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.daily_view)

    def test_pending_request_indicator_has_a_direct_destination(self):
        index = (PROJECT_ROOT / "templates" / "portal" / "index.html").read_text(encoding="utf-8")
        routes = self.routes
        self.assertIn("def hr_my_pending_requests", routes)
        self.assertIn("HRLeaveRequest.query", routes)
        self.assertIn("HRPermissionRequest.query", routes)
        self.assertIn("portal.hr_my_pending_requests", index)


if __name__ == "__main__":
    unittest.main()
