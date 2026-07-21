import unittest
from datetime import datetime
from types import SimpleNamespace

from utils.audit_story import build_audit_story_entries, humanize_audit_note


class AuditStoryTests(unittest.TestCase):
    def test_story_is_chronological_and_human_readable(self):
        user = SimpleNamespace(full_name="أحمد خالد", email="ahmad@example.com")
        newer = SimpleNamespace(
            id=2,
            created_at=datetime(2026, 7, 19, 11, 15),
            action="STEP_APPROVED",
            user=user,
            on_behalf_of_user=None,
            request_id=42,
            target_type="WORKFLOW_STEP",
            target_id=7,
            note="step=2 | source=WORKFLOW",
            old_status="PENDING",
            new_status="APPROVED",
        )
        older = SimpleNamespace(
            id=1,
            created_at=datetime(2026, 7, 19, 9, 0),
            action="USER_LOGIN",
            user=user,
            on_behalf_of_user=None,
            request_id=None,
            target_type=None,
            target_id=None,
            note=None,
            old_status=None,
            new_status=None,
        )

        story = build_audit_story_entries(
            [newer, older],
            request_meta={42: {"request_type": "إجازة", "template_name": "اعتماد الإجازات"}},
            log_steps={2: 2},
        )

        self.assertEqual([entry["id"] for entry in story], [1, 2])
        self.assertIn("سجّل أحمد خالد الدخول", story[0]["sentence"])
        self.assertEqual(story[1]["request_id"], 42)
        self.assertIn("المعاملة رقم 42", story[1]["request_summary"])
        self.assertEqual(story[1]["step"], 2)
        self.assertIn("قيد الانتظار", story[1]["status_sentence"])
        self.assertIn("موافق عليه", story[1]["status_sentence"])

    def test_technical_note_fields_are_translated(self):
        note = humanize_audit_note("file_id=17 | step=3 | source=WORKFLOW")
        self.assertEqual(note, "رقم الملف: 17، الخطوة: 3، المصدر: مسار")

    def test_json_note_is_flattened(self):
        note = humanize_audit_note('{"status": "APPROVED", "reason": "تمت المراجعة"}')
        self.assertEqual(note, "الحالة: موافق عليه، السبب: تمت المراجعة")

    def test_unknown_action_code_is_not_shown_to_non_technical_users(self):
        log = SimpleNamespace(
            id=3,
            created_at=datetime(2026, 7, 19, 12, 0),
            action="ARCHIVE_FINAL_DELETE_RETENTION_JOB",
            user=None,
            on_behalf_of_user=None,
            request_id=None,
            target_type="ARCHIVE_FILE",
            target_id=19,
            note=None,
            old_status=None,
            new_status=None,
        )

        sentence = build_audit_story_entries([log])[0]["sentence"]
        self.assertNotIn("ARCHIVE_FINAL_DELETE_RETENTION_JOB", sentence)
        self.assertNotIn("_", sentence)
        self.assertIn("حذف", sentence)


if __name__ == "__main__":
    unittest.main()
