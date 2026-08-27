import unittest
from pathlib import Path

from jinja2 import Environment


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NEW_REQUEST_TEMPLATE = PROJECT_ROOT / "templates" / "workflow" / "new_request.html"


class WorkflowPrintSummaryTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = NEW_REQUEST_TEMPLATE.read_text(encoding="utf-8")

    def test_new_request_template_is_valid_jinja(self):
        Environment().parse(self.template)

    def test_printing_uses_a_compact_summary_instead_of_the_builder(self):
        self.assertIn('class="workflow-new-screen-only no-print"', self.template)
        self.assertIn('id="workflowNewRequestPrintSummary"', self.template)
        self.assertIn('id="workflowPrintRouteSteps"', self.template)
        self.assertIn("window.addEventListener('beforeprint', syncPrintSummary)", self.template)

    def test_print_summary_keeps_only_useful_request_fields(self):
        for element_id in (
            "workflowPrintTitle",
            "workflowPrintRequestType",
            "workflowPrintPriority",
            "workflowPrintDescription",
            "workflowPrintAttachmentNames",
            "workflowPrintRouteName",
        ):
            self.assertIn(f'id="{element_id}"', self.template)

        summary_markup = self.template.split(
            '<section class="workflow-new-print-summary"', 1
        )[1].split("<script>", 1)[0]
        for builder_label in (
            "طريقة بناء المسار",
            "مساراتي الديناميكية المحفوظة",
            "هل تريد إضافة لجنة إلى المسار؟",
            "الهيكلية",
            "هل تريد إضافة الأمين العام كآخر خطوة؟",
            "حفظ المسار الحالي",
            "إرسال وحفظ",
        ):
            self.assertNotIn(builder_label, summary_markup)


if __name__ == "__main__":
    unittest.main()
