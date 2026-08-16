import unittest
from pathlib import Path

from portal.routes import _corr_normalize_route_tree


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CorrespondenceRouteTreeTests(unittest.TestCase):
    def test_malformed_breadcrumb_roots_are_merged_into_canonical_hierarchy(self):
        canonical = {
            "id": 1,
            "name_ar": "المؤسسة",
            "people": [],
            "children": [
                {
                    "id": 2,
                    "name_ar": "دائرة التعاون الدولي",
                    "people": [],
                    "children": [],
                }
            ],
        }
        malformed = {
            "id": 99,
            "name_ar": "المؤسسة > الأمانة العامة > دائرة التعاون الدولي",
            "people": [{"id": 7, "name": "مستخدم الدائرة", "rank": 3}],
            "children": [],
        }
        duplicate_root = {
            "id": 100,
            "name_ar": "المؤسسة > المؤسسة",
            "people": [{"id": 8, "name": "مستخدم المؤسسة", "rank": 2}],
            "children": [],
        }

        result = _corr_normalize_route_tree([canonical, malformed, duplicate_root])

        self.assertEqual([node["id"] for node in result], [1])
        self.assertEqual([person["id"] for person in result[0]["people"]], [8])
        self.assertEqual([person["id"] for person in result[0]["children"][0]["people"]], [7])

    def test_competence_selects_use_the_bounded_search_picker(self):
        templates = (
            "inbound_new.html",
            "inbound_edit.html",
            "inbound_list.html",
            "outbound_new.html",
            "outbound_edit.html",
            "outbound_list.html",
        )
        for filename in templates:
            source = (PROJECT_ROOT / "templates" / "portal" / "corr" / filename).read_text(
                encoding="utf-8"
            )
            with self.subTest(template=filename):
                self.assertIn('data-searchable-mode="bounded"', source)

        script = (PROJECT_ROOT / "static" / "js" / "searchable_select.js").read_text(
            encoding="utf-8"
        )
        self.assertIn('menu.style.maxWidth = "100%"', script)
        self.assertIn('button.style.whiteSpace = "normal"', script)
        self.assertIn('button.style.overflowWrap = "anywhere"', script)


if __name__ == "__main__":
    unittest.main()
