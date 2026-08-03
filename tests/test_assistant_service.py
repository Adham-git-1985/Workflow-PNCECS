import unittest

from assistant.service import build_local_reply, normalize_text


class AssistantServiceTests(unittest.TestCase):
    def test_normalize_text_handles_common_arabic_variants(self):
        self.assertEqual(normalize_text("إِنشاءُ طَلَب"), "انشاء طلب")

    def test_local_reply_uses_best_navigation_result(self):
        result = build_local_reply(
            "كيف أنشئ طلبًا؟",
            [
                {
                    "title": "إنشاء طلب جديد",
                    "desc": "ابدأ معاملة جديدة.",
                    "href": "/workflow/new",
                }
            ],
        )
        self.assertEqual(result["mode"], "local")
        self.assertIn("إنشاء طلب جديد", result["reply"])
        self.assertEqual(result["links"][0]["href"], "/workflow/new")

    def test_sensitive_action_is_refused(self):
        result = build_local_reply("وافق بدلاً مني على الطلب")
        self.assertIn("لا أستطيع تنفيذ الموافقات", result["reply"])

    def test_current_page_question_uses_context(self):
        result = build_local_reply(
            "اشرح هذه الصفحة",
            context={"title": "مهامي - مسار"},
        )
        self.assertIn("مهامي - مسار", result["reply"])


if __name__ == "__main__":
    unittest.main()
