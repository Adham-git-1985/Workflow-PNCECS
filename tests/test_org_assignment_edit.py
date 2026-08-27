import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from extensions import db
from models import (
    Department,
    Directorate,
    Organization,
    OrgNode,
    OrgNodeAssignment,
    OrgNodeType,
    OrgUnitAssignment,
    Section,
    SystemSetting,
    User,
)
from portal.routes import hr_org_assignments_save, portal_admin_hr_org_structure


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _unwrapped(function):
    while hasattr(function, "__wrapped__"):
        function = function.__wrapped__
    return function


class OrgAssignmentEditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY="test-only",
        )
        db.init_app(cls.app)
        cls.context = cls.app.app_context()
        cls.context.push()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.context.pop()

    def setUp(self):
        db.drop_all()
        db.create_all()

    @staticmethod
    def _user(email, name):
        user = User(email=email, name=name, password_hash="not-used", role="ADMIN")
        db.session.add(user)
        db.session.flush()
        return user

    def test_page_exposes_edit_action_and_assignment_identity(self):
        template = (
            PROJECT_ROOT / "templates" / "portal" / "hr" / "org_assignments.html"
        ).read_text(encoding="utf-8")

        self.assertIn('name="assignment_id"', template)
        self.assertIn("org-assignment-edit", template)
        self.assertIn('data-assignment-id="{{ r.id }}"', template)
        self.assertIn("حفظ التعديل", template)
        self.assertIn("إلغاء التعديل", template)

    def test_edit_moves_the_same_assignment_and_removes_old_dynamic_mirror(self):
        actor = self._user("actor@example.test", "مدير النظام")
        employee = self._user("employee@example.test", "موظف تجريبي")
        organization = Organization(name_ar="المؤسسة", is_active=True)
        db.session.add(organization)
        db.session.flush()
        old_directorate = Directorate(
            organization_id=organization.id,
            name_ar="الإدارة القديمة",
            is_active=True,
        )
        new_directorate = Directorate(
            organization_id=organization.id,
            name_ar="الإدارة الجديدة",
            is_active=True,
        )
        node_type = OrgNodeType(
            code="DIRECTORATE",
            name_ar="إدارة",
            sort_order=10,
            allow_in_approvals=True,
            show_in_chart=True,
            show_in_routes=True,
            is_active=True,
        )
        db.session.add_all([old_directorate, new_directorate, node_type])
        db.session.flush()
        old_node = OrgNode(
            type_id=node_type.id,
            name_ar=old_directorate.name_ar,
            legacy_type="DIRECTORATE",
            legacy_id=old_directorate.id,
            is_active=True,
        )
        assignment = OrgUnitAssignment(
            user_id=employee.id,
            unit_type="DIRECTORATE",
            unit_id=old_directorate.id,
            title="المسمى القديم",
            is_primary=True,
            created_by_id=actor.id,
        )
        db.session.add_all([old_node, assignment])
        db.session.flush()
        dynamic_assignment = OrgNodeAssignment(
            user_id=employee.id,
            node_id=old_node.id,
            title=assignment.title,
            is_primary=True,
        )
        db.session.add(dynamic_assignment)
        employee.org_node_id = old_node.id
        db.session.commit()

        form_data = {
            "assignment_id": str(assignment.id),
            "user_id": str(employee.id),
            "unit_type": "DIRECTORATE",
            "unit_id": str(new_directorate.id),
            "title": "المسمى الجديد",
            "is_primary": "1",
        }
        save = _unwrapped(hr_org_assignments_save)
        with self.app.test_request_context(
            "/portal/hr/org-assignments/save",
            method="POST",
            data=form_data,
        ), patch("portal.routes.current_user", actor), patch(
            "portal.routes._portal_audit"
        ), patch("portal.routes.sync_legacy_now"), patch(
            "portal.routes.url_for", return_value="/portal/hr/org-assignments"
        ):
            response = save()

        db.session.expire_all()
        stored = db.session.get(OrgUnitAssignment, assignment.id)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(OrgUnitAssignment.query.count(), 1)
        self.assertEqual(stored.unit_id, new_directorate.id)
        self.assertEqual(stored.title, "المسمى الجديد")
        self.assertTrue(stored.is_primary)
        self.assertIsNone(
            OrgNodeAssignment.query.filter_by(
                user_id=employee.id,
                node_id=old_node.id,
            ).first()
        )
        self.assertIsNone(db.session.get(User, employee.id).org_node_id)

    def test_section_parent_can_move_between_departments_with_one_parent_value(self):
        organization = Organization(name_ar="المؤسسة", is_active=True)
        db.session.add(organization)
        db.session.flush()
        directorate = Directorate(
            organization_id=organization.id,
            name_ar="الإدارة العامة للمنظمات",
            is_active=True,
        )
        db.session.add(directorate)
        db.session.flush()
        old_department = Department(
            directorate_id=directorate.id,
            name_ar="دائرة الأيسيسكو",
            is_active=True,
        )
        new_department = Department(
            directorate_id=directorate.id,
            name_ar="دائرة الألكسو",
            is_active=True,
        )
        db.session.add_all([old_department, new_department])
        db.session.flush()
        section = Section(
            department_id=old_department.id,
            name_ar="قسم العلاقات الدولية",
            code="INT-REL",
            is_active=True,
        )
        db.session.add(section)
        department_type = OrgNodeType(
            code="DEPARTMENT",
            name_ar="دائرة",
            is_active=True,
        )
        section_type = OrgNodeType(
            code="SECTION",
            name_ar="قسم",
            is_active=True,
        )
        db.session.add_all([department_type, section_type])
        db.session.flush()
        old_department_node = OrgNode(
            type_id=department_type.id,
            name_ar=old_department.name_ar,
            legacy_type="DEPARTMENT",
            legacy_id=old_department.id,
            is_active=True,
        )
        new_department_node = OrgNode(
            type_id=department_type.id,
            name_ar=new_department.name_ar,
            legacy_type="DEPARTMENT",
            legacy_id=new_department.id,
            is_active=True,
        )
        db.session.add_all([old_department_node, new_department_node])
        db.session.flush()
        section_node = OrgNode(
            type_id=section_type.id,
            parent_id=old_department_node.id,
            name_ar=section.name_ar,
            code=section.code,
            legacy_type="SECTION",
            legacy_id=section.id,
            is_active=True,
        )
        db.session.add_all([
            section_node,
            SystemSetting(
                key="ORG_APPROVED_STRUCTURE_VERSION",
                value="2023-05-08:v2",
            ),
        ])
        db.session.commit()

        form_data = {
            "op": "save",
            "kind": "secs",
            "id": str(section.id),
            "parent_ref": f"department:{new_department.id}",
            "code": section.code,
            "name_ar": section.name_ar,
            "name_en": "International Relations",
            "is_active": "on",
        }
        section_id = section.id
        new_department_id = new_department.id
        section_node_id = section_node.id
        new_department_node_id = new_department_node.id
        # Match a real HTTP request: the target parent is not already present
        # in the session identity map, so resolving it would normally trigger
        # autoflush.
        db.session.expunge_all()
        save = _unwrapped(portal_admin_hr_org_structure)
        with self.app.test_request_context(
            "/portal/admin/hr/org-structure?tab=secs",
            method="POST",
            data=form_data,
        ), patch("portal.routes._legacy_org_locked", return_value=False), patch(
            "portal.routes.url_for", return_value="/portal/admin/hr/org-structure?tab=secs"
        ):
            response = save()

        db.session.expire_all()
        stored = db.session.get(Section, section_id)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(stored.department_id, new_department_id)
        self.assertIsNone(stored.directorate_id)
        self.assertIsNone(stored.unit_id)
        self.assertEqual(
            db.session.get(OrgNode, section_node_id).parent_id,
            new_department_node_id,
        )

    def test_section_form_uses_a_single_explicit_parent_field(self):
        template = (
            PROJECT_ROOT / "templates" / "portal" / "admin" / "hr_org_structure.html"
        ).read_text(encoding="utf-8")

        self.assertIn('name="parent_ref" form="{{ section_form_id }}"', template)
        self.assertIn('value="department:{{ d.id }}"', template)
        self.assertIn('form="{{ section_form_id }}"', template)


if __name__ == "__main__":
    unittest.main()
