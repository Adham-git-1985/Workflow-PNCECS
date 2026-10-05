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

        self.app.register_blueprint(workflow, url_prefix="/workflow")

    def test_notification_poll_is_not_audited(self):
        with self.app.test_client() as client:
            response = client.get("/workflow/notifications/poll")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"should_audit": False})
        self.assertIn(
            "workflow.poll_notifications",
            HIGH_FREQUENCY_REQUEST_ENDPOINTS,
        )

    def test_regular_page_view_remains_audited(self):
        with self.app.test_client() as client:
            response = client.get("/workflow/inbox")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"should_audit": True})


if __name__ == "__main__":
    unittest.main()
