import unittest
from pathlib import Path

from jinja2 import Environment


class HRPrintViewTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]
    PRINT_TEMPLATES = (
        "attendance_daily.html",
        "attendance_events.html",
        "attendance_absence.html",
        "reports_delay.html",
        "work_schedule_all.html",
    )

    def test_print_base_is_a4_and_hides_controls(self):
        template = (self.ROOT / "templates/portal/hr/print/base.html").read_text(encoding="utf-8")

        self.assertIn("@page { size: A4", template)
        self.assertIn(".print-toolbar { display: none !important; }", template)
        self.assertNotIn("portal/hr/base.html", template)

    def test_all_print_templates_are_standalone_report_children(self):
        environment = Environment()
        for filename in self.PRINT_TEMPLATES:
            with self.subTest(filename=filename):
                template = (self.ROOT / "templates/portal/hr/print" / filename).read_text(encoding="utf-8")
                environment.parse(template)
                self.assertIn('{% extends "portal/hr/print/base.html" %}', template)

    def test_attendance_pages_link_to_filtered_print_views(self):
        templates = (
            "attendance_daily.html",
            "attendance_events.html",
            "attendance_absence.html",
            "reports_delay.html",
            "work_schedule.html",
        )
        for filename in templates:
            with self.subTest(filename=filename):
                template = (self.ROOT / "templates/portal/hr" / filename).read_text(encoding="utf-8")
                self.assertIn("print=1", template)
                self.assertIn('target="_blank"', template)

    def test_all_employee_schedule_prints_both_weeks(self):
        template = (self.ROOT / "templates/portal/hr/print/work_schedule_all.html").read_text(encoding="utf-8")

        self.assertIn("organization_print_weeks", template)
        self.assertIn('class="print-page"', template)
        self.assertIn("الأسبوع", template)


if __name__ == "__main__":
    unittest.main()
