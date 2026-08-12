import unittest
from io import BytesIO
from pathlib import Path
import tempfile
import zipfile

from docx import Document

from services.meeting_service import (
    add_embedded_attachments_to_docx,
    normalize_agenda_order,
    recorded_attendance_label,
    validate_docx_package,
    validate_embedded_attachments,
)
from services.ole_package import build_ole_package


class MeetingAgendaOrderTests(unittest.TestCase):
    def test_accepts_complete_reordering(self):
        self.assertEqual(
            normalize_agenda_order(["30", "10", "20"], [10, 20, 30]),
            [30, 10, 20],
        )

    def test_rejects_stale_or_invalid_submissions(self):
        invalid_submissions = [
            ["10", "10", "20"],
            ["10", "20"],
            ["10", "20", "99"],
            ["10", "not-an-id", "30"],
        ]
        for submitted in invalid_submissions:
            with self.subTest(submitted=submitted):
                with self.assertRaises(ValueError):
                    normalize_agenda_order(submitted, [10, 20, 30])


class MeetingMinutesTests(unittest.TestCase):
    def test_recorded_attendance_has_only_attended_or_absent_labels(self):
        self.assertEqual(recorded_attendance_label("ATTENDED"), "حضر")
        self.assertEqual(recorded_attendance_label("ABSENT"), "تغيب")
        self.assertEqual(recorded_attendance_label("INVITED"), "تغيب")

    def test_validate_docx_package_accepts_required_parts(self):
        output = BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types />")
            archive.writestr("_rels/.rels", "<Relationships />")
            archive.writestr("word/document.xml", "<document />")
        validate_docx_package(output.getvalue())

    def test_validate_docx_package_rejects_incomplete_file(self):
        with self.assertRaises(ValueError):
            validate_docx_package(b"not a docx")

    def test_ole_package_contains_original_payload_and_unicode_filename(self):
        payload = b"unique-meeting-attachment-payload"
        blob = build_ole_package("تقرير اللجنة.pdf", payload)
        self.assertTrue(blob.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"))
        self.assertIn(payload, blob)
        self.assertIn("تقرير اللجنة.pdf".encode("utf-16-le"), blob)

    def test_docx_contains_every_attachment_as_an_ole_object(self):
        icon_path = Path(__file__).resolve().parents[1] / "static" / "images" / "pncecs_logo.png"
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            first = temp_path / "first.pdf"
            second = temp_path / "second.xlsx"
            first_payload = b"%PDF-test-embedded-one"
            second_payload = b"PK-test-embedded-two"
            first.write_bytes(first_payload)
            second.write_bytes(second_payload)

            document = Document()
            count = add_embedded_attachments_to_docx(
                document,
                [("تقرير أول.pdf", first), ("جدول ثان.xlsx", second)],
                icon_path=icon_path,
            )
            output = BytesIO()
            document.save(output)
            docx_data = output.getvalue()

        self.assertEqual(count, 2)
        validate_embedded_attachments(docx_data, 2)
        with zipfile.ZipFile(BytesIO(docx_data)) as archive:
            document_xml = archive.read("word/document.xml").decode("utf-8")
            rels_xml = archive.read("word/_rels/document.xml.rels").decode("utf-8")
            embedded_names = sorted(
                name for name in archive.namelist()
                if name.startswith("word/embeddings/oleObject")
            )
            embedded_blobs = [archive.read(name) for name in embedded_names]

        self.assertEqual(len(embedded_blobs), 2)
        self.assertEqual(document_xml.count("<o:OLEObject"), 2)
        self.assertIn("relationships/oleObject", rels_xml)
        self.assertTrue(any(first_payload in blob for blob in embedded_blobs))
        self.assertTrue(any(second_payload in blob for blob in embedded_blobs))

    def test_missing_attachment_fails_instead_of_silently_omitting_it(self):
        document = Document()
        icon_path = Path(__file__).resolve().parents[1] / "static" / "images" / "pncecs_logo.png"
        with self.assertRaises(FileNotFoundError):
            add_embedded_attachments_to_docx(
                document,
                [("missing.pdf", Path("missing-meeting-attachment.pdf"))],
                icon_path=icon_path,
            )


if __name__ == "__main__":
    unittest.main()
