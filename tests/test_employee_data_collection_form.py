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


if __name__ == "__main__":
    unittest.main()
