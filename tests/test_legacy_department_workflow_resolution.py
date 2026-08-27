import unittest

from flask import Flask

from extensions import db
from models import (
    Department,
    Directorate,
    Organization,
    OrgNode,
    OrgNodeManager,
    OrgNodeType,
    User,
    WorkflowTemplate,
    WorkflowTemplateStep,
)
from workflow.engine import resolve_template_parallel_candidate_user_ids


class LegacyDepartmentWorkflowResolutionTests(unittest.TestCase):
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

    def test_legacy_education_department_resolves_canonical_node_deputy(self):
        organization = Organization(name_ar="الأمانة العامة", is_active=True)
        db.session.add(organization)
        db.session.flush()
        directorate = Directorate(
            organization_id=organization.id,
            name_ar="الإدارة العامة المتخصصة",
            is_active=True,
        )
        db.session.add(directorate)
        db.session.flush()
        legacy_department = Department(
            directorate_id=directorate.id,
            name_ar="دائرة التربية",
            is_active=True,
        )
        node_type = OrgNodeType(
            code="DEPARTMENT",
            name_ar="دائرة",
            sort_order=60,
            allow_in_approvals=True,
            show_in_chart=True,
            show_in_routes=True,
            is_active=True,
        )
        deputy = User(
            email="noor@example.test",
            name="نور برغوثي",
            password_hash="not-used",
            role="dept_head",
        )
        db.session.add_all([legacy_department, node_type, deputy])
        db.session.flush()

        canonical_node = OrgNode(
            type_id=node_type.id,
            name_ar="دائرة التربية والتعليم العالي",
            # Older approved-structure imports did not stamp the canonical
            # code, matching the production backup covered by this regression.
            code=None,
            is_active=True,
        )
        db.session.add(canonical_node)
        db.session.flush()

        # A legacy sync may also create an exact DEPARTMENT/id node.  It has no
        # manager; the approved node below is the operational source of truth.
        db.session.add(OrgNode(
            type_id=node_type.id,
            name_ar="دائرة التربية",
            legacy_type="DEPARTMENT",
            legacy_id=legacy_department.id,
            is_active=True,
        ))
        db.session.add(OrgNodeManager(
            node_id=canonical_node.id,
            deputy_user_id=deputy.id,
        ))

        template = WorkflowTemplate(name="مسار فحص رأي تخصصي", is_active=True)
        db.session.add(template)
        db.session.flush()
        db.session.add(WorkflowTemplateStep(
            template_id=template.id,
            step_order=1,
            mode="PARALLEL_SYNC",
            approver_kind="DEPARTMENT",
            approver_department_id=legacy_department.id,
        ))
        db.session.commit()

        self.assertEqual(
            resolve_template_parallel_candidate_user_ids(template, 1),
            [deputy.id],
        )


if __name__ == "__main__":
    unittest.main()
