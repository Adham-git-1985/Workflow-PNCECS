import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from assistant.knowledge import _correspondence_matches, assistant_access_profile
from assistant.service import _try_external_ai, build_local_reply, normalize_text


class _FakeUser:
    def __init__(self, role):
        self.role = role
        self.id = 42

    def has_role(self, role):
        mine = (self.role or "").upper().replace("-", "_")
        wanted = (role or "").upper().replace("-", "_")
        if mine in {"SUPER_ADMIN", "SUPERADMIN"}:
            return wanted in {"SUPER_ADMIN", "SUPERADMIN", "ADMIN"}
        return mine == wanted


class _FakeCorrespondence:
    def __init__(self, item_id, subject):
        self.id = item_id
        self.subject = subject
        self.ref_no = None
        self.sender = None
        self.recipient = None
        self.competence_label = None


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

    def test_help_overview_does_not_require_a_well_formed_question(self):
        result = build_local_reply("لا أعرف من أين أبدأ")
        self.assertIn("لا تحتاج إلى معرفة صياغة السؤال", result["reply"])
        self.assertIn("حل مشكلة", result["reply"])

    def test_missing_button_gets_actionable_troubleshooting(self):
        result = build_local_reply("لا يظهر لي الزر الذي أحتاجه")
        self.assertIn("اختفاء زر أو خيار", result["reply"])
        self.assertIn("الخطوة الحالية", result["reply"])

    def test_stuck_request_gets_troubleshooting_steps(self):
        result = build_local_reply("الطلب متوقف ولا ينتقل للخطوة التالية")
        self.assertIn("للتحقق من طلب متوقف", result["reply"])
        self.assertIn("حالة الطلب رقم 123", result["reply"])

    def test_local_mode_responds_to_ordinary_conversation(self):
        result = build_local_reply("كيف حالك؟")
        self.assertIn("أنا بخير", result["reply"])
        self.assertIn("كيف حالك أنت", result["reply"])

    def test_local_mode_responds_supportively_to_difficulty(self):
        result = build_local_reply("أنا مش فاهم والموضوع صعب")
        self.assertIn("خلّينا نبسّطها معًا", result["reply"])
        self.assertIn("خطوة خطوة", result["reply"])

    def test_local_follow_up_acknowledges_conversation_history(self):
        result = build_local_reply(
            "وضح أكثر",
            history=[{"role": "assistant", "content": "هذا شرح سابق."}],
        )
        self.assertIn("ردي السابق", result["reply"])

    def test_unknown_local_chat_explains_open_conversation_requirement(self):
        result = build_local_reply("حدثني عن أي موضوع عام")
        self.assertIn("وصلتني رسالتك", result["reply"])
        self.assertIn("وضع الذكاء الاصطناعي", result["reply"])

    def test_external_chat_receives_history_and_privacy_controls(self):
        app = Flask(__name__)
        app.config.update(
            SECRET_KEY="test-secret",
            ASSISTANT_AI_ENABLED="1",
            ASSISTANT_OPENAI_API_KEY="test-key",
            ASSISTANT_OPENAI_MODEL="test-model",
            ASSISTANT_AI_MAX_OUTPUT_TOKENS=400,
        )
        client = unittest.mock.MagicMock()
        client.responses.create.return_value = SimpleNamespace(output_text="رد تفاعلي")
        with app.app_context(), patch("openai.OpenAI", return_value=client):
            reply = _try_external_ai(
                _FakeUser("EMPLOYEE"),
                "وماذا بعد؟",
                [{"role": "user", "content": "أريد متابعة الطلب"}],
                {},
                [],
                {},
            )

        self.assertEqual(reply, "رد تفاعلي")
        payload = client.responses.create.call_args.kwargs
        self.assertFalse(payload["store"])
        self.assertTrue(payload["safety_identifier"].startswith("aref_"))
        self.assertEqual(payload["input"][0]["content"], "أريد متابعة الطلب")
        self.assertIn("تابع سياق الرسائل السابقة", payload["instructions"])

    def test_current_page_question_uses_context(self):
        result = build_local_reply(
            "اشرح هذه الصفحة",
            context={"title": "مهامي - مسار"},
        )
        self.assertIn("مهامي - مسار", result["reply"])

    def test_local_reply_prefers_permission_scoped_knowledge(self):
        result = build_local_reply(
            "ما هي صلاحياتي؟",
            knowledge={
                "reply": "حسابك ونطاق عارف\nالدور: موظف.",
                "access_level": "employee",
                "access_label": "نطاق المستخدم وصلاحياته",
            },
        )
        self.assertIn("نطاق عارف", result["reply"])
        self.assertEqual(result["access_level"], "employee")

    def test_local_reply_exposes_retrieval_sources_and_admin_suggestions(self):
        result = build_local_reply(
            "اشرح هيكلية المشروع",
            knowledge={
                "reply": "معرفة المشروع",
                "access_level": "super_admin",
                "access_label": "نطاق سوبر أدمن",
                "sources": [{"label": "app.py:1", "path": "app.py", "line": 1}],
                "index_stats": {"indexed_files": 10},
            },
        )
        self.assertEqual(result["sources"][0]["label"], "app.py:1")
        self.assertIn("ما جداول قاعدة البيانات؟", result["suggestions"])
        self.assertEqual(result["index_stats"]["indexed_files"], 10)

    def test_aref_never_elevates_admin_scope(self):
        self.assertEqual(assistant_access_profile(_FakeUser("ADMIN"))["level"], "admin")
        self.assertEqual(assistant_access_profile(_FakeUser("SUPER_ADMIN"))["level"], "super_admin")
        self.assertEqual(assistant_access_profile(_FakeUser("EMPLOYEE"))["level"], "employee")

    def test_exact_correspondence_id_does_not_match_other_records(self):
        item = _FakeCorrespondence(8, "موضوع ظاهر")
        self.assertFalse(_correspondence_matches(item, [], 9))
        self.assertTrue(_correspondence_matches(item, [], 8))


if __name__ == "__main__":
    unittest.main()
