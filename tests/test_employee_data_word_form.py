import io
import unittest
import zipfile
from xml.etree import ElementTree as ET

from services.employee_data_word_form import (
    PLACEHOLDER,
    W_NS,
    build_employee_word_form,
    parse_employee_word_form,
)


class EmployeeDataWordFormTests(unittest.TestCase):
    def test_generated_word_form_has_stable_content_control_tags(self):
        document = build_employee_word_form()
        with zipfile.ZipFile(io.BytesIO(document)) as archive:
            root = ET.fromstring(archive.read("word/document.xml"))
        tags = [node.get(f"{{{W_NS}}}val") for node in root.findall(f".//{{{W_NS}}}tag")]
        self.assertIn("f:timeclock_code", tags)
        self.assertIn("f:national_id", tags)
        self.assertIn("t:dependent:1:full_name", tags)
        self.assertIn("t:qualification:1:degree_lookup_id", tags)

    def test_word_parser_returns_the_existing_json_schema(self):
        payload = parse_employee_word_form(build_employee_word_form())
        self.assertEqual(payload["schema"], "EMP-DATA-FORM/V1.1")
        self.assertEqual(payload["fields"], {})
        self.assertEqual(payload["tables"], {})
        self.assertNotIn(PLACEHOLDER, str(payload))


if __name__ == "__main__":
    unittest.main()
