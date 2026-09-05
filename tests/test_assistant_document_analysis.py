import unittest
from io import BytesIO
from unittest.mock import MagicMock, patch

from flask import Flask
from werkzeug.datastructures import FileStorage

from assistant.document_analysis import analyze_uploaded_attachment
from assistant.service import summarize_content


class _FakeUser:
    id = 42


class AssistantDocumentAnalysisTests(unittest.TestCase):
    def test_text_attachment_is_extracted_in_memory(self):
        app = Flask(__name__)
        app.config.update(
            ASSISTANT_ANALYSIS_MAX_FILE_BYTES=1024 * 1024,
            ASSISTANT_ANALYSIS_MAX_TEXT_CHARS=5000,
            ASSISTANT_ANALYSIS_MAX_PDF_PAGES=10,
            ASSISTANT_ANALYSIS_OCR_ENABLED=False,
        )
        upload = FileStorage(
            stream=BytesIO("First point. Second point.".encode("utf-8")),
            filename="notes.txt",
            content_type="text/plain",
        )

        with app.app_context():
            result = analyze_uploaded_attachment(upload)

        self.assertEqual(result["filename"], "notes.txt")
        self.assertEqual(result["format"], "Text")
        self.assertIn("First point", result["text"])

    def test_fallback_summary_never_uses_external_ai(self):
        app = Flask(__name__)
        app.config.update(
            ASSISTANT_LOCAL_AI_ENABLED="0",
            ASSISTANT_AI_ENABLED="1",
            ASSISTANT_OPENAI_API_KEY="test-key",
            ASSISTANT_OPENAI_MODEL="test-model",
        )
        with app.app_context(), patch(
            "assistant.service._try_external_ai",
            side_effect=AssertionError("documents must remain local"),
        ):
            result = summarize_content(
                _FakeUser(),
                "The meeting starts at 10:00. The team approved the plan. Follow up on Monday.",
                instruction="Summarize the main decisions.",
                source_label="meeting.txt",
            )

        self.assertEqual(result["mode"], "local")
        self.assertIn("meeting.txt", result["reply"])
        self.assertIn("The meeting starts", result["reply"])

    def test_actions_and_draft_fallback_is_review_only_and_never_uses_external_ai(self):
        app = Flask(__name__)
        app.config.update(
            ASSISTANT_LOCAL_AI_ENABLED="0",
            ASSISTANT_AI_ENABLED="1",
            ASSISTANT_OPENAI_API_KEY="test-key",
            ASSISTANT_OPENAI_MODEL="test-model",
        )
        with app.app_context(), patch(
            "assistant.service._try_external_ai",
            side_effect=AssertionError("documents must remain local"),
        ):
            result = summarize_content(
                _FakeUser(),
                "Please send the report to Finance by 10 October. The manager must review it first.",
                source_label="request.txt",
                analysis_mode="actions_draft",
            )

        self.assertEqual(result["mode"], "local")
        self.assertIn("المهام أو الإجراءات المستخرجة", result["reply"])
        self.assertIn("مسودة للمراجعة فقط — غير مرسلة", result["reply"])
        self.assertNotIn("sent", result["reply"].lower())
        self.assertIn("actions_draft", result["intents"])

    def test_local_model_receives_the_document_but_external_model_is_not_called(self):
        app = Flask(__name__)
        app.config.update(
            ASSISTANT_LOCAL_AI_ENABLED="1",
            ASSISTANT_LOCAL_AI_MODEL="qwen2.5:3b",
            ASSISTANT_LOCAL_AI_URL="http://127.0.0.1:11434/api/chat",
            ASSISTANT_LOCAL_AI_TIMEOUT=10,
            ASSISTANT_ANALYSIS_MODEL_CONTEXT_CHARS=4000,
        )
        opener = MagicMock()
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"message":{"content":"Local document summary."}}'
        opener.open.return_value = response

        with (
            app.app_context(),
            patch("assistant.service.build_opener", return_value=opener),
            patch(
                "assistant.service._try_external_ai",
                side_effect=AssertionError("documents must remain local"),
            ),
        ):
            result = summarize_content(
                _FakeUser(),
                "Private document content that must stay on this server.",
                instruction="Summarize it.",
                source_label="private.txt",
            )

        self.assertEqual(result["mode"], "local_ai")
        self.assertEqual(result["reply"], "Local document summary.")
        request = opener.open.call_args.args[0]
        self.assertIn(b"Private document content", request.data)

    def test_local_model_gets_review_only_actions_and_draft_instructions(self):
        app = Flask(__name__)
        app.config.update(
            ASSISTANT_LOCAL_AI_ENABLED="1",
            ASSISTANT_LOCAL_AI_MODEL="qwen2.5:3b",
            ASSISTANT_LOCAL_AI_URL="http://127.0.0.1:11434/api/chat",
            ASSISTANT_LOCAL_AI_TIMEOUT=10,
        )
        opener = MagicMock()
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"message":{"content":"Draft review."}}'
        opener.open.return_value = response

        with app.app_context(), patch("assistant.service.build_opener", return_value=opener):
            result = summarize_content(
                _FakeUser(),
                "يرجى مراجعة الطلب قبل 10/10.",
                source_label="letter.pdf",
                analysis_mode="actions_draft",
            )

        self.assertEqual(result["reply"], "Draft review.")
        request = opener.open.call_args.args[0]
        self.assertIn("مسودة للمراجعة".encode("utf-8"), request.data)
        self.assertIn("لا تنشئ طلبًا أو مهمة".encode("utf-8"), request.data)

    def test_long_content_is_summarized_in_local_parts_before_the_final_summary(self):
        app = Flask(__name__)
        app.config.update(
            ASSISTANT_LOCAL_AI_ENABLED="1",
            ASSISTANT_LOCAL_AI_MODEL="qwen2.5:3b",
            ASSISTANT_LOCAL_AI_URL="http://127.0.0.1:11434/api/chat",
            ASSISTANT_LOCAL_AI_TIMEOUT=10,
            ASSISTANT_ANALYSIS_MODEL_CONTEXT_CHARS=4000,
        )
        opener = MagicMock()
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"message":{"content":"Part summary."}}'
        opener.open.return_value = response
        long_content = "Important record. " * 400

        with app.app_context(), patch("assistant.service.build_opener", return_value=opener):
            result = summarize_content(
                _FakeUser(),
                long_content,
                instruction="Summarize the whole document.",
                source_label="long.txt",
            )

        self.assertEqual(result["mode"], "local_ai")
        self.assertEqual(result["reply"], "Part summary.")
        self.assertGreater(opener.open.call_count, 1)


if __name__ == "__main__":
    unittest.main()
