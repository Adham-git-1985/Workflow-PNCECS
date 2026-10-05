import unittest

from flask import Blueprint, Flask, jsonify

from utils.request_audit import (
    HIGH_FREQUENCY_REQUEST_ENDPOINTS,
    _should_audit_request,
)


class AutomatedRequestAuditTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        workflow = Blueprint("workflow", __name__)

        @workflow.get("/notifications/poll")
        def poll_notifications():
            return jsonify({"should_audit": _should_audit_request()})

        @workflow.get("/inbox")
        def inbox():
            return jsonify({"should_audit": _should_audit_request()})

        @workflow.post("/approve")
        def approve():
            return jsonify({"should_audit": _should_audit_request()})

        chats = Blueprint("chats", __name__)

        @chats.get("/unread-count")
        def unread_count():
            return jsonify({"should_audit": _should_audit_request()})

        @chats.get("/alerts")
        def chat_alerts():
            return jsonify({"should_audit": _should_audit_request()})

        @self.app.get("/logout")
        def logout():
            return jsonify({"should_audit": _should_audit_request()})

        self.app.register_blueprint(workflow, url_prefix="/workflow")
        self.app.register_blueprint(chats, url_prefix="/chats")

    def test_notification_poll_is_not_audited(self):
        with self.app.test_client() as client:
            response = client.get("/workflow/notifications/poll")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"should_audit": False})
        self.assertIn(
            "workflow.poll_notifications",
            HIGH_FREQUENCY_REQUEST_ENDPOINTS,
        )

    def test_private_chat_polling_is_not_audited(self):
        with self.app.test_client() as client:
            unread_response = client.get("/chats/unread-count")
            alerts_response = client.get("/chats/alerts")

        self.assertEqual(unread_response.status_code, 200)
        self.assertEqual(alerts_response.status_code, 200)
        self.assertEqual(unread_response.get_json(), {"should_audit": False})
        self.assertEqual(alerts_response.get_json(), {"should_audit": False})
        self.assertIn("chats.unread_count", HIGH_FREQUENCY_REQUEST_ENDPOINTS)
        self.assertIn("chats.chat_alerts", HIGH_FREQUENCY_REQUEST_ENDPOINTS)

    def test_regular_page_view_is_not_audited(self):
        with self.app.test_client() as client:
            response = client.get("/workflow/inbox")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"should_audit": False})

    def test_state_change_and_logout_remain_audited(self):
        with self.app.test_client() as client:
            action_response = client.post("/workflow/approve")
            logout_response = client.get("/logout")

        self.assertEqual(action_response.status_code, 200)
        self.assertEqual(action_response.get_json(), {"should_audit": True})
        self.assertEqual(logout_response.status_code, 200)
        self.assertEqual(logout_response.get_json(), {"should_audit": True})


if __name__ == "__main__":
    unittest.main()
