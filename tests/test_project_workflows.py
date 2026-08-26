import unittest

from flask import Flask

from extensions import db
from models import (
    OrgNode,
    OrgNodeManager,
    OrgNodeType,
    RequestType,
    User,
    WorkflowRoutingRule,
    WorkflowTemplate,
    WorkflowTemplateStep,
)
from workflow.project_workflows import (
    PROJECT_WORKFLOW_DEFINITIONS,
    PROJECT_WORKFLOW_METADATA_BY_TEMPLATE_NAME,
    ProjectWorkflowConfigurationError,
    upsert_project_workflows,
)
from workflow.dynamic_paths import org_node_approver_names


class ProjectWorkflowSeedTests(unittest.TestCase):
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

        node_type = OrgNodeType(
            code="DIRECTORATE",
            name_ar="إدارة عامة",
            sort_order=10,
            allow_in_approvals=True,
            show_in_chart=True,
            show_in_routes=True,
            is_active=True,
        )
        db.session.add(node_type)
        db.session.flush()
        db.session.add_all((
            OrgNode(
                type_id=node_type.id,
                name_ar="الإدارة العامة للبرامج والمشاريع",
                code="DIR_PROGRAMS",
                is_active=True,
            ),
            OrgNode(
                type_id=node_type.id,
                name_ar="مساعد الأمين العام للبرامج والمشاريع",
                code="ASST_PROGRAMS",
                is_active=True,
            ),
            User(
                email="admin@example.test",
                name="مدير النظام",
                password_hash="not-used",
                role="ADMIN",
            ),
        ))
        db.session.commit()

    def test_seed_creates_three_scoped_project_workflows(self):
        results = upsert_project_workflows()
        db.session.commit()

        self.assertEqual(len(results), 3)
        self.assertEqual(WorkflowTemplate.query.count(), 3)
        self.assertEqual(RequestType.query.count(), 3)
        self.assertEqual(WorkflowRoutingRule.query.count(), 3)

        expected_names = {definition.template_name for definition in PROJECT_WORKFLOW_DEFINITIONS}
        self.assertEqual(
            {template.name for template in WorkflowTemplate.query.all()},
            expected_names,
        )
        self.assertEqual(
            PROJECT_WORKFLOW_METADATA_BY_TEMPLATE_NAME["مسار المشاريع الجديدة"]["description"],
            "من التسجيل حتى الاعتماد.",
        )
        projects_node = OrgNode.query.filter_by(code="DIR_PROGRAMS").one()
        assistant_node = OrgNode.query.filter_by(code="ASST_PROGRAMS").one()

        for definition in PROJECT_WORKFLOW_DEFINITIONS:
            template = WorkflowTemplate.query.filter_by(name=definition.template_name).one()
            steps = WorkflowTemplateStep.query.filter_by(template_id=template.id).order_by(
                WorkflowTemplateStep.step_order.asc()
            ).all()
            self.assertEqual(steps[0].approver_kind, "ORG_NODE")
            self.assertEqual(steps[0].approver_org_node_id, projects_node.id)
            self.assertEqual(steps[1].approver_kind, "ORG_NODE")
            self.assertEqual(steps[1].approver_org_node_id, assistant_node.id)
            self.assertEqual(len(steps), 3 if definition.include_secretary_general else 2)
            if definition.include_secretary_general:
                self.assertEqual(steps[2].approver_kind, "ROLE")
                self.assertEqual(steps[2].approver_role, "General_secretary")
            self.assertTrue(all(step.sla_days == 3 for step in steps))

            request_type = RequestType.query.filter_by(code=definition.request_type_code).one()
            rule = WorkflowRoutingRule.query.filter_by(
                request_type_id=request_type.id,
                template_id=template.id,
            ).one()
            self.assertEqual(rule.org_node_id, projects_node.id)
            self.assertTrue(rule.match_subtree)
            self.assertTrue(rule.is_active)

    def test_seed_is_idempotent(self):
        upsert_project_workflows()
        db.session.commit()
        first_ids = sorted(template.id for template in WorkflowTemplate.query.all())

        upsert_project_workflows()
        db.session.commit()

        self.assertEqual(
            sorted(template.id for template in WorkflowTemplate.query.all()),
            first_ids,
        )
        self.assertEqual(WorkflowRoutingRule.query.count(), 3)
        self.assertEqual(WorkflowTemplateStep.query.count(), 8)

    def test_startup_seed_preserves_admin_template_edits(self):
        upsert_project_workflows(preserve_existing=True)
        db.session.commit()

        self.assertEqual(WorkflowTemplate.query.count(), 3)

        template = WorkflowTemplate.query.filter_by(
            name="مسار المشاريع القائمة"
        ).one()
        request_type = RequestType.query.filter_by(code="PROJECT_ACTIVE").one()
        rule = WorkflowRoutingRule.query.filter_by(
            request_type_id=request_type.id,
            template_id=template.id,
        ).one()
        first_step = WorkflowTemplateStep.query.filter_by(
            template_id=template.id,
            step_order=1,
        ).one()

        template.sla_days_default = 9
        template.is_active = False
        request_type.name_ar = "اسم معدل"
        request_type.is_active = False
        rule.priority = 17
        first_step.sla_days = 11
        db.session.commit()

        upsert_project_workflows(preserve_existing=True)
        db.session.commit()

        self.assertEqual(template.sla_days_default, 9)
        self.assertFalse(template.is_active)
        self.assertEqual(request_type.name_ar, "اسم معدل")
        self.assertFalse(request_type.is_active)
        self.assertEqual(rule.priority, 17)
        self.assertEqual(first_step.sla_days, 11)
        self.assertEqual(
            WorkflowTemplateStep.query.filter_by(template_id=template.id).count(),
            2,
        )

    def test_seed_requires_approved_project_nodes(self):
        OrgNode.query.filter_by(code="ASST_PROGRAMS").delete(synchronize_session=False)
        db.session.commit()

        with self.assertRaises(ProjectWorkflowConfigurationError):
            upsert_project_workflows()

    def test_org_node_approver_names_exposes_assigned_person(self):
        projects_node = OrgNode.query.filter_by(code="DIR_PROGRAMS").one()
        user = User.query.filter_by(email="admin@example.test").one()
        db.session.add(OrgNodeManager(
            node_id=projects_node.id,
            manager_user_id=user.id,
        ))
        db.session.commit()

        labels = org_node_approver_names([projects_node.id])

        self.assertEqual(labels[projects_node.id], "مدير النظام")


if __name__ == "__main__":
    unittest.main()
