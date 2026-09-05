import tempfile
import unittest
from io import BytesIO
from unittest.mock import patch

from flask import Flask

from assistant import assistant_bp
from extensions import db, login_manager
from models import User


class AssistantAnalysisRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.app = Flask(__name__)
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="assistant-analysis-route-test",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            ASSISTANT_ANALYSIS_MAX_FILE_BYTES=1024 * 1024,
            ASSISTANT_ANALYSIS_MAX_TEXT_CHARS=5000,
            ASSISTANT_ANALYSIS_OCR_ENABLED=False,
        )
        db.init_app(cls.app)
        login_manager.init_app(cls.app)
        login_manager.login_view = "login"

        @login_manager.user_loader
        def load_user(user_id):
            try:
                return db.session.get(User, int(user_id))
            except (TypeError, ValueError):
                return None

        cls.app.register_blueprint(assistant_bp)
        cls.context = cls.app.app_context()
        cls.context.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.context.pop()
        cls.temp_dir.cleanup()

    def setUp(self):
        db.session.query(User).delete()
        self.user = User(
            email="assistant@example.test",
            name="Assistant User",
            password_hash="unused",
            role="EMPLOYEE",
        )
        db.session.add(self.user)
        db.session.commit()

    def _client(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(self.user.id)
            session["_fresh"] = True
        return client

    def test_pasted_long_text_uses_analysis_route(self):
        client = self._client()
        expected = {
            "reply": "Local summary",
            "mode": "local",
            "links": [],
            "sources": [],
            "suggestions": [],
        }
        with (
            patch("assistant.routes.validate_csrf"),
            patch("assistant.routes.summarize_content", return_value=expected.copy()) as summarize,
        ):
            response = client.post(
                "/api/assistant/analyze",
                json={"text": "A long text to summarize.", "instruction": "Summarize it."},
                headers={"X-CSRFToken": "test"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["reply"], "Local summary")
        self.assertEqual(payload["analysis"]["kind"], "text")
        self.assertEqual(summarize.call_args.args[1], "A long text to summarize.")

    def test_attachment_is_extracted_then_sent_to_local_summary(self):
        client = self._client()
        expected = {
            "reply": "Local attachment summary",
            "mode": "local",
            "links": [],
            "sources": [],
            "suggestions": [],
        }
        with (
            patch("assistant.routes.validate_csrf"),
            patch("assistant.routes.summarize_content", return_value=expected.copy()) as summarize,
        ):
            response = client.post(
                "/api/assistant/analyze",
                data={
                    "file": (BytesIO(b"First item. Second item."), "notes.txt"),
                    "instruction": "Summarize the attachment.",
                },
                content_type="multipart/form-data",
                headers={"X-CSRFToken": "test"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["analysis"]["kind"], "attachment")
        self.assertEqual(payload["analysis"]["format"], "Text")
        self.assertIn("First item", summarize.call_args.args[1])

    def test_actions_and_draft_mode_is_passed_to_local_only_analysis(self):
        client = self._client()
        expected = {
            "reply": "Review-only draft",
            "mode": "local",
            "links": [],
            "sources": [],
            "suggestions": [],
        }
        with (
            patch("assistant.routes.validate_csrf"),
            patch("assistant.routes.summarize_content", return_value=expected.copy()) as summarize,
        ):
            response = client.post(
                "/api/assistant/analyze",
                json={
                    "text": "يرجى إرسال التقرير قبل نهاية الأسبوع.",
                    "analysis_mode": "actions_draft",
                },
                headers={"X-CSRFToken": "test"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["analysis"]["mode"], "actions_draft")
        self.assertEqual(summarize.call_args.kwargs["analysis_mode"], "actions_draft")


if __name__ == "__main__":
    unittest.main()
