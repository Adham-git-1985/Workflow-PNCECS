import unittest

from utils.ui_labels import ui_label, ui_text, workflow_status_label


class UiLabelsArabicTests(unittest.TestCase):
    def test_workflow_approved_uses_follow_up_wording_only_in_workflows(self):
        self.assertEqual(workflow_status_label("APPROVED"), "تم الاطلاع والمتابعة")
        self.assertEqual(workflow_status_label("REJECTED"), "تم توقيف المسار")
        self.assertEqual(ui_label("STEP_REJECTED"), "توقيف المسار")
        self.assertEqual(ui_label("APPROVED"), "موافق عليه")

    def test_requested_audit_and_category_codes_are_arabic(self):
        self.assertEqual(
            ui_label("CORR_CONFIDENTIAL_ACCESS_UPDATE"),
            "تحديث صلاحيات الوصول إلى مراسلة سرية",
        )
        self.assertEqual(ui_label("GENERAL"), "عام")

    def test_role_codes_are_arabic_regardless_of_case_style(self):
        self.assertEqual(ui_label("directorate_head"), "مدير عام الإدارة")
        self.assertEqual(ui_label("dept_head"), "رئيس الدائرة")
        self.assertEqual(ui_label("General_secretary"), "الأمين العام")

    def test_camel_case_and_workflow_codes_are_arabic(self):
        self.assertEqual(ui_label("WorkflowRequest"), "طلب مسار")
        self.assertEqual(ui_label("Message"), "رسالة")
        self.assertEqual(ui_label("PARALLEL_SYNC"), "متزامن")
        self.assertEqual(
            ui_label("PARALLEL_SYNC_AUTHORIZED"),
            "توجيه خطوة متزامنة إلى المعنيين",
        )

    def test_codes_embedded_in_user_facing_text_are_translated(self):
        translated = ui_text(
            "CORR_CONFIDENTIAL_ACCESS_UPDATE GENERAL directorate_head dept_head"
        )
        self.assertNotIn("CORR_CONFIDENTIAL_ACCESS_UPDATE", translated)
        self.assertNotIn("GENERAL", translated)
        self.assertNotIn("directorate_head", translated)
        self.assertNotIn("dept_head", translated)
        self.assertIn("تحديث صلاحيات الوصول إلى مراسلة سرية", translated)
        self.assertIn("عام", translated)
        self.assertIn("مدير عام الإدارة", translated)
        self.assertIn("رئيس الدائرة", translated)

    def test_unknown_free_text_is_preserved(self):
        self.assertEqual(ui_label("نص مخصص"), "نص مخصص")

    def test_common_historical_audit_notes_are_arabic(self):
        timeclock_note = ui_text(
            "TIMECLK sync inserted=15 skipped=4 errors=0 summaries=15"
        )
        self.assertEqual(
            timeclock_note,
            "مزامنة جهاز الدوام: تمت إضافة=15 تم التجاهل=4 "
            "الأخطاء=0 الملخصات=15",
        )

        role_note = ui_text("ROLE changed from employee to dept_head")
        self.assertEqual(
            role_note,
            "دور وظيفي تغيّر من موظف إلى رئيس الدائرة",
        )


if __name__ == "__main__":
    unittest.main()
