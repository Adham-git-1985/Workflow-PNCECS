import unittest

from flask import Flask

from extensions import db
from models import Organization, OrgNode, OrgNodeAssignment, OrgNodeManager, OrgNodeType, User
from utils.approved_org_structure import (
    APPROVED_LEGACY_TYPE,
    apply_approved_org_structure,
    flatten_approved_structure,
)
from utils.org_dynamic import build_chart_tree, sync_legacy_now


class ApprovedOrgStructureTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_complete_chart_contains_information_department_and_divisions(self):
        first = apply_approved_org_structure()
        db.session.commit()

        expected = len(flatten_approved_structure())
        self.assertEqual(first["expected"], expected)
        self.assertEqual(
            OrgNode.query.filter_by(legacy_type=APPROVED_LEGACY_TYPE, is_active=True).count(),
            expected,
        )

        roots = OrgNode.query.filter_by(parent_id=None, is_active=True).all()
        self.assertEqual([node.code for node in roots], ["ORG_PNCECS"])

        info = OrgNode.query.filter_by(code="DEP_INFORMATION", is_active=True).one()
        self.assertEqual(info.parent.code, "DIR_INFO_PUB")
        self.assertEqual(
            {child.code for child in info.children if child.is_active},
            {"SEC_INFORMATION", "SEC_ANALYTICS", "SEC_E_ARCHIVE"},
        )

        chart = build_chart_tree()
        flattened = []

        def walk(nodes):
            for node in nodes:
                flattened.append(node)
                walk(node["children"])

        walk(chart)
        self.assertEqual(len(chart), 1)
        self.assertEqual(len(flattened), expected)
        self.assertIn("DIVISION", {node["type"] for node in flattened})

        international_relations = OrgNode.query.filter_by(
            code="SEC_ICESCO_REL",
            is_active=True,
        ).one()
        self.assertEqual(international_relations.parent.code, "DEP_ALECSO")

        second = apply_approved_org_structure()
        db.session.commit()
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["merged"], 0)
        self.assertEqual(OrgNode.query.filter_by(is_active=True).count(), expected)

        legacy_org = Organization(name_ar="Legacy structure must stay read-only", code="LOCK-CHECK")
        db.session.add(legacy_org)
        db.session.commit()
        self.assertFalse(sync_legacy_now(raise_errors=True))
        self.assertIsNone(
            OrgNode.query.filter_by(legacy_type="ORGANIZATION", legacy_id=legacy_org.id).first()
        )

    def test_duplicate_import_artifact_is_merged_without_losing_references(self):
        org_type = OrgNodeType(
            code="ORGANIZATION",
            name_ar="منظمة",
            sort_order=10,
            is_active=True,
            show_in_chart=True,
            show_in_routes=True,
            allow_in_approvals=True,
        )
        department_type = OrgNodeType(
            code="DEPARTMENT",
            name_ar="دائرة",
            sort_order=40,
            is_active=True,
            show_in_chart=True,
            show_in_routes=True,
            allow_in_approvals=True,
        )
        db.session.add_all([org_type, department_type])
        db.session.flush()

        canonical = OrgNode(type_id=department_type.id, name_ar="دائرة الايسيسكو", is_active=True)
        duplicate = OrgNode(
            type_id=org_type.id,
            name_ar=(
                "اللجنة الوطنية للتربية وللثقافة والعلوم>الامين العام>"
                "مساعد الامين العام للمنظمات والبرامج والمشاريع (الادارات التخصصية)>"
                "الادارة العامة للمنظمات العربية والاسلامية والعلاقات الدولية >دائرة الايسيسكو"
            ),
            is_active=True,
        )
        user = User(
            email="org-test@example.test",
            name="Org Test",
            role="EMPLOYEE",
            password_hash="not-used-in-test",
        )
        duplicate_manager = User(
            email="duplicate-manager@example.test",
            name="Duplicate Manager",
            role="EMPLOYEE",
            password_hash="not-used-in-test",
        )
        db.session.add_all([canonical, duplicate, user, duplicate_manager])
        db.session.flush()
        db.session.add_all([
            OrgNodeAssignment(user_id=user.id, node_id=canonical.id, is_primary=False),
            OrgNodeAssignment(user_id=user.id, node_id=duplicate.id, is_primary=True),
            OrgNodeManager(node_id=canonical.id, manager_user_id=user.id),
            OrgNodeManager(node_id=duplicate.id, manager_user_id=duplicate_manager.id),
        ])
        user.org_node_id = duplicate.id
        db.session.commit()

        canonical_id = canonical.id
        duplicate_id = duplicate.id
        apply_approved_org_structure()
        db.session.commit()

        canonical = db.session.get(OrgNode, canonical_id)
        duplicate = db.session.get(OrgNode, duplicate_id)
        self.assertEqual(canonical.code, "DEP_ICESCO")
        self.assertTrue(canonical.is_active)
        self.assertFalse(duplicate.is_active)
        self.assertEqual(user.org_node_id, canonical.id)
        assignment = OrgNodeAssignment.query.filter_by(user_id=user.id, node_id=canonical.id).one()
        self.assertTrue(assignment.is_primary)
        self.assertEqual(OrgNodeAssignment.query.filter_by(user_id=user.id).count(), 1)
        manager = OrgNodeManager.query.filter_by(node_id=canonical.id).one()
        self.assertEqual(manager.manager_user_id, user.id)
        self.assertEqual(manager.deputy_user_id, duplicate_manager.id)


if __name__ == "__main__":
    unittest.main()
