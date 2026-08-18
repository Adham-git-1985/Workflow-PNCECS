import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class EmployeeDataCollectionFormTests(unittest.TestCase):
    def setUp(self):
        self.template = (
            PROJECT_ROOT
            / "templates"
            / "portal"
            / "hr"
            / "employee_data_collection_form.html"
        ).read_text(encoding="utf-8")

    def test_route_and_employee_list_link_are_registered(self):
        routes = (PROJECT_ROOT / "portal" / "routes.py").read_text(encoding="utf-8")
        employees = (
            PROJECT_ROOT / "templates" / "portal" / "hr" / "employees.html"
        ).read_text(encoding="utf-8")

        self.assertIn('@portal_bp.route("/hr/employees/data-collection-form")', routes)
        self.assertIn("def hr_employee_data_collection_form():", routes)
        self.assertIn("portal.hr_employee_data_collection_form", employees)

        route_start = routes.index('@portal_bp.route("/hr/employees/data-collection-form")')
        route_end = routes.index("def _employee_upload_dir", route_start)
        route_block = routes[route_start:route_end]
        self.assertIn("@login_required", route_block)
        self.assertNotIn("@_perm(HR_EMP_READ)", route_block)

    def test_form_is_a_six_page_a4_print_document(self):
        self.assertIn("@page", self.template)
        self.assertIn("size: A4 portrait", self.template)
        self.assertIn("window.print()", self.template)
        self.assertEqual(self.template.count('class="paper-page"'), 6)

    def test_employee_file_fields_have_machine_readable_keys(self):
        expected_fields = {
            "employee_no",
            "full_name_quad",
            "timeclock_code",
            "identity_type_lookup_id",
            "national_id",
            "gender_lookup_id",
            "marital_status_lookup_id",
            "birth_date",
            "religion_lookup_id",
            "disability_lookup_id",
            "home_governorate_lookup_id",
            "locality_lookup_id",
            "address",
            "phone",
            "mobile",
            "email",
            "work_governorate_lookup_id",
            "work_location_lookup_id",
            "employee_status_lookup_id",
            "status_date",
            "status_note",
            "shift_lookup_id",
            "hourly_number",
            "organization_id",
            "directorate_id",
            "department_id",
            "division_id",
            "direct_manager_user_id",
            "project_lookup_id",
            "appointment_type_lookup_id",
            "hire_date",
            "last_promotion_date",
            "job_category_lookup_id",
            "job_grade_lookup_id",
            "job_title_lookup_id",
            "admin_title_lookup_id",
            "bank_lookup_id",
            "bank_account",
            "notes",
        }

        for field in expected_fields:
            with self.subTest(field=field):
                self.assertIn(f'data-field="{field}"', self.template)

    def test_repeating_records_and_privacy_guidance_are_included(self):
        for field in (
            "dependent.full_name",
            "dependent.allowance",
            "qualification.degree_lookup_id",
            "qualification.notes",
            "secondment.date_from",
            "secondment.details",
            "attachment.attachment_type_lookup_id",
            "attachment.note",
        ):
            with self.subTest(field=field):
                self.assertIn(f'data-field="{field}"', self.template)

        self.assertIn("لا تكتب كلمة المرور", self.template)
        self.assertIn("ظرف مغلق", self.template)

    def test_existing_employee_entry_screens_accept_collected_notes(self):
        routes = (PROJECT_ROOT / "portal" / "routes.py").read_text(encoding="utf-8")
        basic = (
            PROJECT_ROOT / "templates" / "portal" / "hr" / "employee" / "basic.html"
        ).read_text(encoding="utf-8")
        qualification = (
            PROJECT_ROOT
            / "templates"
            / "portal"
            / "hr"
            / "employee"
            / "qualification_form.html"
        ).read_text(encoding="utf-8")

        self.assertIn('textarea name="notes"', basic)
        self.assertIn('input type="text" name="notes"', qualification)
        self.assertIn("notes=_to_str(request.form.get('notes'))", routes)
        self.assertIn("qual.notes = _to_str(request.form.get('notes'))", routes)

    def test_form_supports_device_entry_drafts_and_json_handoff(self):
        for feature in (
            'id="saveDraftButton"',
            'id="downloadJsonButton"',
            'id="importJsonInput"',
            'id="clearFormButton"',
            'contentEditable = "true"',
            "localStorage.setItem",
            "localStorage.getItem",
            'new Blob([JSON.stringify(payload, null, 2)]',
            'type: "application/json;charset=utf-8"',
            'const FORM_SCHEMA = "EMP-DATA-FORM/V1.1"',
        ):
            with self.subTest(feature=feature):
                self.assertIn(feature, self.template)

    def test_online_and_offline_handoff_are_staged_for_hr_review(self):
        routes = (PROJECT_ROOT / "portal" / "routes.py").read_text(encoding="utf-8")
        inbox = (
            PROJECT_ROOT / "templates" / "portal" / "hr" / "employee_data_submissions.html"
        ).read_text(encoding="utf-8")
        review = (
            PROJECT_ROOT / "templates" / "portal" / "hr" / "employee_data_submission_view.html"
        ).read_text(encoding="utf-8")

        self.assertIn('id="submitOnlineButton"', self.template)
        self.assertIn("portal.hr_employee_data_collection_form_offline", self.template)
        self.assertIn("function submitOnline()", self.template)
        self.assertIn('@portal_bp.route("/hr/employee-data-submissions/online", methods=["POST"])', routes)
        self.assertIn('@portal_bp.route("/hr/employee-data-submissions/upload", methods=["POST"])', routes)
        self.assertIn('@portal_bp.route("/hr/employee-data-submissions/<int:submission_id>/apply", methods=["POST"])', routes)
        self.assertIn("@_perm(HR_EMP_MANAGE)", routes)
        self.assertIn("رفع إجابات نموذج عُبّئ من الجهاز", inbox)
        self.assertIn("اعتماد وترحيل إلى ملف الموظف", review)
        self.assertIn("رفض دون ترحيل", review)

    def test_offline_download_embeds_images_and_removes_server_actions(self):
        self.assertIn("offline_pncecs_logo", self.template)
        self.assertIn("offline_masar_logo", self.template)
        self.assertIn("{% if not offline_mode %}", self.template)
        self.assertIn('data-submit-url="{{ \'\' if offline_mode', self.template)

    def test_employee_self_service_links_to_interactive_form(self):
        me_home = (
            PROJECT_ROOT / "templates" / "portal" / "hr" / "me_home.html"
        ).read_text(encoding="utf-8")
        self.assertIn("استكمال بياناتي", me_home)
        self.assertIn("portal.hr_employee_data_collection_form", me_home)

    def test_both_secondment_rows_keep_machine_readable_fields(self):
        self.assertEqual(
            self.template.count('data-field="secondment.date_from"'),
            2,
        )
        self.assertEqual(
            self.template.count('data-field="secondment.details"'),
            2,
        )

    def test_hourly_number_has_an_adjacent_identity_reference(self):
        hourly_index = self.template.index('data-field="hourly_number"')
        identity_index = self.template.index('data-field="page_2_national_id"')
        self.assertLess(hourly_index, identity_index)
        self.assertIn("مرجع للرقم في الساعة", self.template)
        self.assertIn('["national_id", /^page_\\d+_national_id$/]', self.template)

    def test_timeclock_code_explains_which_identifier_to_enter(self):
        self.assertIn('data-field="timeclock_code"', self.template)
        self.assertIn("كود ساعة الدوام (رقم الهوية أو الرقم الوظيفي)", self.template)
        self.assertIn("اكتب الرقم المستخدم فعليًا في ساعة الدوام.", self.template)


if __name__ == "__main__":
    unittest.main()
