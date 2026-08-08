import tempfile
import unittest
from pathlib import Path

from assistant.project_knowledge import (
    build_project_index,
    internal_knowledge_allowed,
)


class _FakeUser:
    def __init__(self, role):
        self.role = role

    def has_role(self, role):
        mine = (self.role or "").upper().replace("-", "_")
        wanted = (role or "").upper().replace("-", "_")
        return mine == wanted


class AssistantProjectKnowledgeTests(unittest.TestCase):
    def test_index_searches_code_and_redacts_literal_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "module.py").write_text(
                'OPENAI_API_KEY = "real-secret"\n\n'
                "app.config['SECRET_KEY'] = 'config-secret'\n\n"
                'print("ADMIN -> admin@example.test / 123")\n\n'
                "def calculate_leave_balance(employee_id):\n"
                "    return employee_id * 2\n",
                encoding="utf-8",
            )
            (root / "README.md").write_text("# Leave workflow\nEmployee leave guide", encoding="utf-8")
            (root / ".env").write_text("OPENAI_API_KEY=never-index-me", encoding="utf-8")

            index = build_project_index(root, chunk_lines=16)
            matches = index.search("calculate_leave_balance", limit=3)

            self.assertTrue(matches)
            self.assertEqual(matches[0][1].path, "module.py")
            all_text = "\n".join(chunk.text for chunk in index.chunks)
            self.assertNotIn("real-secret", all_text)
            self.assertNotIn("config-secret", all_text)
            self.assertNotIn("admin@example.test", all_text)
            self.assertNotIn("never-index-me", all_text)
            self.assertIn("[REDACTED]", all_text)

    def test_index_keeps_binary_file_names_without_reading_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "assets").mkdir()
            (root / "assets" / "leave-template.docx").write_bytes(b"not-a-real-docx")

            index = build_project_index(root)
            matches = index.search("leave template", limit=5)

            self.assertEqual(index.discovered_files, 1)
            self.assertEqual(index.indexed_files, 0)
            self.assertTrue(any(chunk.kind == "file_manifest" for _, chunk in matches))

    def test_internal_knowledge_is_admin_only(self):
        self.assertTrue(internal_knowledge_allowed(_FakeUser("ADMIN")))
        self.assertTrue(internal_knowledge_allowed(_FakeUser("SUPER_ADMIN")))
        self.assertFalse(internal_knowledge_allowed(_FakeUser("EMPLOYEE")))

    def test_domain_file_is_ranked_above_a_matching_suggestion_literal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "assistant").mkdir()
            (root / "assistant" / "service.py").write_text(
                '_SUGGESTIONS = ("كيف يعمل نظام الصلاحيات في الكود؟",)\n',
                encoding="utf-8",
            )
            (root / "permissions.py").write_text(
                "def has_permission(user, permission):\n"
                "    return permission in user.permissions\n",
                encoding="utf-8",
            )

            index = build_project_index(root, chunk_lines=16)
            matches = index.search("كيف يعمل نظام الصلاحيات في الكود؟", limit=2)

            self.assertEqual(matches[0][1].path, "permissions.py")


if __name__ == "__main__":
    unittest.main()
