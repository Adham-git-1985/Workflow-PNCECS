import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PortalAccessRequestLinkTests(unittest.TestCase):
    def test_pending_request_cancel_forms_use_registered_endpoint(self):
        template = (PROJECT_ROOT / "templates" / "portal" / "index.html").read_text(
            encoding="utf-8"
        )
        routes = (PROJECT_ROOT / "portal" / "routes.py").read_text(encoding="utf-8")

        self.assertNotIn("portal.cancel_access_request", template)
        self.assertEqual(template.count("portal.my_access_request_cancel"), 5)
        self.assertIn("def my_access_request_cancel(req_id: int):", routes)


if __name__ == "__main__":
    unittest.main()
