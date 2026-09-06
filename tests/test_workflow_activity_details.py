import unittest
from types import SimpleNamespace

from workflow.routes import (
    MENTION_ACCESS_ACTION,
    MENTION_ACCESS_REVOKED_ACTION,
    _user_facing_audit_note,
)


class WorkflowActivityDetailsTests(unittest.TestCase):
    def test_attachment_activity_shows_original_filename(self):
        log = SimpleNamespace(
            target_id=18,
            note="Attachment: دوام-أيلول.pdf | file_id=18 | step=2 | source=COMMENT",
        )
        files_map = {
            18: SimpleNamespace(original_name="دوام-أيلول.pdf", stored_name="random.pdf"),
        }

        detail = _user_facing_audit_note(log, "WORKFLOW_ATTACHMENT_UPLOADED", files_map)

        self.assertEqual(detail, "اسم المرفق: دوام-أيلول.pdf")

    def test_mention_activity_shows_the_mentioned_person_name(self):
        log = SimpleNamespace(
            target_id=7,
            note="step=1 | المستخدم المشار إليه=رحيق داوود صادق شتية",
        )

        detail = _user_facing_audit_note(log, MENTION_ACCESS_ACTION, {})

        self.assertEqual(detail, "المستخدم المذكور: رحيق داوود صادق شتية")

    def test_removed_mention_keeps_the_mentioned_person_name(self):
        log = SimpleNamespace(
            target_id=7,
            note="تمت إزالة المستخدم المشار إليه=رحيق داوود صادق شتية",
        )

        detail = _user_facing_audit_note(log, MENTION_ACCESS_REVOKED_ACTION, {})

        self.assertEqual(detail, "المستخدم المذكور: رحيق داوود صادق شتية")


if __name__ == "__main__":
    unittest.main()
