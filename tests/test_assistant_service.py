import unittest

from assistant.knowledge import _correspondence_matches, assistant_access_profile
from assistant.service import build_local_reply, normalize_text


class _FakeUser:
    def __init__(self, role):
        self.role = role

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
