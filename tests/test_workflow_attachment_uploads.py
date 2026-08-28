import unittest
from io import BytesIO

from flask import Flask

from workflow.routes import _uploaded_files_from_request


class WorkflowAttachmentUploadTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)

    def test_collects_standard_and_scanner_attachments(self):
        with self.app.test_request_context(
            "/workflow/new",
            method="POST",
            data={
                "files": (BytesIO(b"standard attachment"), "standard.pdf"),
                "scanned_files": (BytesIO(b"scanned attachment"), "scanned.pdf"),
            },
            content_type="multipart/form-data",
        ):
            uploads = _uploaded_files_from_request("files", "scanned_files")

        self.assertEqual(
            [upload.filename for upload in uploads],
            ["standard.pdf", "scanned.pdf"],
        )


if __name__ == "__main__":
    unittest.main()
