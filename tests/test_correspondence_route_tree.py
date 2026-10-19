import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from portal.routes import _corr_normalize_route_tree, _corr_workflow_runtime_steps


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

    def test_secretariat_correspondence_route_freezes_the_selected_directorate(self):
        template = SimpleNamespace(
            name="مسار_امانة_إدارات",
            steps=[
                SimpleNamespace(
                    id=1,
                    step_order=1,
                    mode="SEQUENTIAL",
                    approver_kind="ROLE",
                    approver_role="General_secretary",
                    approver_user_id=None,
                    approver_department_id=None,
                    approver_directorate_id=None,
                    approver_unit_id=None,
                    approver_section_id=None,
                    approver_division_id=None,
                    approver_org_node_id=None,
                    approver_committee_id=None,
                    committee_delivery_mode=None,
                    sla_days=None,
                ),
                SimpleNamespace(
                    id=3,
                    step_order=3,
                    mode="PARALLEL_SYNC",
                    approver_kind="DIRECTORATE",
                    approver_role=None,
                    approver_user_id=None,
                    approver_department_id=None,
                    approver_directorate_id=13,
                    approver_unit_id=None,
                    approver_section_id=None,
                    approver_division_id=None,
                    approver_org_node_id=None,
                    approver_committee_id=None,
                    committee_delivery_mode=None,
                    sla_days=None,
                ),
            ],
        )
        item = SimpleNamespace(
            current_target_kind="DIRECTORATE",
            current_target_id=5,
            current_target_label="الإدارة العامة للشؤون الإدارية والمالية (إدارة)",
            competence_kind="DIRECTORATE",
            competence_id=5,
            competence_label="الإدارة العامة للشؤون الإدارية والمالية (إدارة)",
        )

        with patch(
            "portal.routes._corr_is_all_directorates",
            side_effect=lambda value: int(value or 0) == 13,
        ):
            runtime_steps = _corr_workflow_runtime_steps(template, item)

        self.assertIsInstance(runtime_steps, list)
        routed_step = runtime_steps[1]
        self.assertEqual(routed_step["approver_kind"], "DIRECTORATE")
        self.assertEqual(routed_step["approver_directorate_id"], 5)
        self.assertIsNone(routed_step["approver_user_id"])
        self.assertIn("الإدارة العامة للشؤون الإدارية والمالية", routed_step["label"])


if __name__ == "__main__":
    unittest.main()
