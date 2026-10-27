import unittest

from flask import Flask

from extensions import db
from models import (
    Department,
    Directorate,
    EmployeeFile,
    Organization,
    Section,
    User,
    WorkflowTemplate,
    WorkflowTemplateStartAccess,
)
from workflow.template_access import (
    START_ACCESS_ALL,
    START_ACCESS_NONE,
    START_ACCESS_SELECTED,
    can_user_start_template,
    user_organization_scope,
)


class WorkflowTemplateStartAccessTests(unittest.TestCase):
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

        self.organization_a = Organization(name_ar="الإدارة العامة الأولى")
        self.organization_b = Organization(name_ar="الإدارة العامة الثانية")
        db.session.add_all([self.organization_a, self.organization_b])
        db.session.flush()

        self.directorate_a = Directorate(
            organization_id=self.organization_a.id,
            name_ar="الدائرة الأولى",
        )
        self.directorate_b = Directorate(
            organization_id=self.organization_b.id,
            name_ar="الدائرة الثانية",
        )
        db.session.add_all([self.directorate_a, self.directorate_b])
        db.session.flush()

        self.department_a = Department(
            directorate_id=self.directorate_a.id,
            name_ar="القسم الأول",
        )
        self.department_b = Department(
            directorate_id=self.directorate_b.id,
            name_ar="القسم الثاني",
        )
        db.session.add_all([self.department_a, self.department_b])
        db.session.flush()
        self.section_a = Section(
            department_id=self.department_a.id,
            name_ar="القسم الفرعي الأول",
        )
        db.session.add(self.section_a)
        db.session.flush()

        self.organization_user = self._user("organization@example.test")
        self.directorate_user = self._user("directorate@example.test")
        self.department_user = self._user("department@example.test")
        self.section_user = self._user("section@example.test")
        self.direct_user = self._user("direct@example.test")
        self.denied_user = self._user("denied@example.test")

        db.session.add_all([
            EmployeeFile(
                user_id=self.organization_user.id,
                organization_id=self.organization_a.id,
            ),
            EmployeeFile(
                user_id=self.directorate_user.id,
                directorate_id=self.directorate_b.id,
            ),
            EmployeeFile(
                user_id=self.department_user.id,
                department_id=self.department_a.id,
            ),
            EmployeeFile(
                user_id=self.section_user.id,
                section_id=self.section_a.id,
            ),
        ])
        db.session.commit()

    @staticmethod
    def _user(email):
        user = User(email=email, password_hash="not-used", role="EMPLOYEE")
        db.session.add(user)
        db.session.flush()
        return user

    def _template(self, mode, rules=()):
        template = WorkflowTemplate(
            name="قالب اختبار",
            start_access_mode=mode,
        )
        db.session.add(template)
        db.session.flush()
        db.session.add_all([
            WorkflowTemplateStartAccess(
                template_id=template.id,
                audience_type=audience_type,
                audience_id=audience_id,
            )
            for audience_type, audience_id in rules
        ])
        db.session.commit()
        return template

    def test_none_blocks_and_all_allows(self):
        template = self._template(START_ACCESS_NONE)
        self.assertFalse(can_user_start_template(self.organization_user, template))

        template.start_access_mode = START_ACCESS_ALL
        db.session.commit()
        self.assertTrue(can_user_start_template(self.denied_user, template))

    def test_selected_mode_supports_mixed_organizations_and_people(self):
        template = self._template(
            START_ACCESS_SELECTED,
            rules=(
                ("ORGANIZATION", self.organization_a.id),
                ("DIRECTORATE", self.directorate_b.id),
                ("DEPARTMENT", self.department_a.id),
                ("SECTION", self.section_a.id),
                ("USER", self.direct_user.id),
            ),
        )

        self.assertTrue(can_user_start_template(self.organization_user, template))
        self.assertTrue(can_user_start_template(self.directorate_user, template))
        self.assertTrue(can_user_start_template(self.department_user, template))
        self.assertTrue(can_user_start_template(self.section_user, template))
        self.assertTrue(can_user_start_template(self.direct_user, template))
        self.assertFalse(can_user_start_template(self.denied_user, template))

    def test_department_membership_includes_parent_directorate_and_organization(self):
        scope = user_organization_scope(self.department_user)
        self.assertIn(("DEPARTMENT", self.department_a.id), scope)
        self.assertIn(("DIRECTORATE", self.directorate_a.id), scope)
        self.assertIn(("ORGANIZATION", self.organization_a.id), scope)

        template = self._template(
            START_ACCESS_SELECTED,
            rules=(("ORGANIZATION", self.organization_a.id),),
        )
        self.assertTrue(can_user_start_template(self.department_user, template))


if __name__ == "__main__":
    unittest.main()
