import unittest
from io import BytesIO
import zipfile

from services.meeting_service import (
    normalize_agenda_order,
    recorded_attendance_label,
    validate_docx_package,
)


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


if __name__ == "__main__":
    unittest.main()
