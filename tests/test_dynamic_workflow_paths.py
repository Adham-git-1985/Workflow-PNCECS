import unittest

from flask import Flask

from extensions import db
from models import (
    AuditLog,
    Notification,
    OrgNode,
    OrgNodeAssignment,
    OrgNodeManager,
    OrgNodeType,
    OrgUnitAssignment,
    Team,
    TeamMembership,
    User,
    UserDynamicWorkflowPreset,
    WorkflowInstance,
    WorkflowInstanceStep,
    WorkflowRequest,
    WorkflowTemplate,
    WorkflowTemplateStep,
)
from workflow.dynamic_paths import (
    FINAL_SECRETARY_GENERAL_REF,
    administration_anchor_id,
    build_dynamic_target_path,
    build_dynamic_user_path,
    build_structural_template_path,
    dynamic_org_browser_nodes,
    dynamic_user_choices,
    hierarchy_position_label,
    node_chain,
    node_path_label,
    same_administration,
    structural_route_nodes,
)
from workflow.engine import (
    decide_step,
    resolve_dynamic_branch_steps,
    resolve_step_approver_user_ids,
    start_workflow_for_request,
)
from utils.org_dynamic import resolve_user_org_node_id, sync_legacy_now
from admin.masterdata import _build_node_tree_for_picker


class DynamicWorkflowPathTests(unittest.TestCase):
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

        self.requester = self._user("requester@example.test", "صاحب الطلب")
        self.same_target = self._user("same@example.test", "موظف من الإدارة نفسها")
        self.cross_target = self._user("cross@example.test", "موظف من إدارة أخرى")
        self.source_manager = self._user("source-manager@example.test", "مدير جهة المصدر")
        self.root_manager = self._user("root-manager@example.test", "مدير الجذر")
        self.target_manager = self._user("target-manager@example.test", "مدير جهة الهدف")

        organization_type = self._node_type("ORGANIZATION", "منظمة", 10)
        directorate_type = self._node_type("DIRECTORATE", "إدارة", 20)
        department_type = self._node_type("DEPARTMENT", "دائرة", 30)

        self.root = self._node("المنظمة", organization_type)
        self.directorate_a = self._node("الإدارة الأولى", directorate_type, self.root)
        self.directorate_b = self._node("الإدارة الثانية", directorate_type, self.root)
        self.department_a1 = self._node("دائرة المصدر", department_type, self.directorate_a)
        self.department_a2 = self._node("دائرة أفقية", department_type, self.directorate_a)
        self.department_b = self._node("دائرة الهدف", department_type, self.directorate_b)

        self.requester.org_node_id = self.department_a1.id
        self.same_target.org_node_id = self.department_a2.id
        self.cross_target.org_node_id = self.department_b.id
        self.source_manager.org_node_id = self.department_a1.id
        self.root_manager.org_node_id = self.root.id
        self.target_manager.org_node_id = self.department_b.id

        db.session.add_all([
            OrgNodeManager(node_id=self.department_a1.id, manager_user_id=self.source_manager.id),
            OrgNodeManager(node_id=self.root.id, manager_user_id=self.root_manager.id),
            OrgNodeManager(node_id=self.department_b.id, manager_user_id=self.target_manager.id),
        ])
        db.session.commit()

    @staticmethod
    def _user(email: str, name: str) -> User:
        user = User(email=email, name=name, password_hash="not-used", role="EMPLOYEE")
        db.session.add(user)
        db.session.flush()
        return user

    @staticmethod
    def _node_type(code: str, name: str, order: int) -> OrgNodeType:
        node_type = OrgNodeType(
            code=code,
            name_ar=name,
            sort_order=order,
            allow_in_approvals=True,
            show_in_chart=True,
            show_in_routes=True,
            is_active=True,
        )
        db.session.add(node_type)
        db.session.flush()
        return node_type

    @staticmethod
    def _node(name: str, node_type: OrgNodeType, parent: OrgNode | None = None) -> OrgNode:
        node = OrgNode(
            type_id=node_type.id,
            parent_id=parent.id if parent else None,
            name_ar=name,
            is_active=True,
        )
        db.session.add(node)
        db.session.flush()
        return node

    def test_same_administration_user_is_selected_directly(self):
        result = build_dynamic_user_path(self.requester, [self.same_target.id])

        self.assertEqual(result["errors"], [])
        self.assertEqual([step["approver_user_id"] for step in result["steps"]], [self.same_target.id])
        self.assertIn("اختيار مباشر", result["steps"][0]["reason"])

    def test_dynamic_org_browser_exposes_all_active_entities_and_manager_context(self):
        empty_department = self._node("دائرة بلا موظفين", self.department_a1.type, self.directorate_a)
        db.session.commit()

        nodes = dynamic_org_browser_nodes(dynamic_user_choices(self.requester), self.requester)
        nodes_by_id = {node["id"]: node for node in nodes}

        self.assertIn(self.root.id, nodes_by_id)
        self.assertIn(self.directorate_a.id, nodes_by_id)
        self.assertIn(self.department_a2.id, nodes_by_id)
        self.assertIn(empty_department.id, nodes_by_id)
        self.assertEqual(nodes_by_id[self.department_a2.id]["direct_user_count"], 1)
        self.assertTrue(nodes_by_id[self.department_a1.id]["can_select"])
        self.assertEqual(
            nodes_by_id[self.department_a1.id]["manager_user_id"],
            self.source_manager.id,
        )
        self.assertEqual(
            [option["id"] for option in nodes_by_id[self.department_a1.id]["route_start_options"]],
            [self.department_a1.id],
        )
        self.assertFalse(nodes_by_id[empty_department.id]["can_select"])
        self.assertGreaterEqual(
            nodes_by_id[self.directorate_a.id]["total_user_count"],
            nodes_by_id[self.department_a2.id]["direct_user_count"],
        )

    def test_hierarchy_position_uses_the_workflow_step_node(self):
        second_managed_node = self._node(
            "دائرة إضافية",
            self.department_a1.type,
            self.directorate_a,
        )
        db.session.add(OrgNodeManager(
            node_id=second_managed_node.id,
            deputy_user_id=self.source_manager.id,
        ))
        db.session.commit()

        self.assertEqual(
            hierarchy_position_label(
                self.source_manager,
                routing_node_label=node_path_label(self.department_a1),
            ),
            "مدير دائرة: دائرة المصدر",
        )
        self.assertEqual(
            hierarchy_position_label(
                self.source_manager,
                routing_node_label=node_path_label(second_managed_node),
            ),
            "نائب مدير دائرة: دائرة إضافية",
        )

    def test_multiple_sibling_entities_can_be_added_in_selected_order(self):
        db.session.add(OrgNodeManager(
            node_id=self.department_a2.id,
            manager_user_id=self.same_target.id,
        ))
        db.session.commit()

        result = build_dynamic_target_path(self.requester, [
            f"NODE:{self.department_a1.id}",
            f"NODE:{self.department_a2.id}",
        ])

        self.assertEqual(result["errors"], [])
        self.assertEqual(
            [step["approver_kind"] for step in result["steps"]],
            ["ORG_NODE", "ORG_NODE"],
        )
        self.assertEqual(
            [step["approver_org_node_id"] for step in result["steps"]],
            [self.department_a1.id, self.department_a2.id],
        )
        self.assertEqual(
            [segment["target_kind"] for segment in result["segments"]],
            ["NODE", "NODE"],
        )

    def test_scoped_node_route_excludes_top_two_levels_and_crosses_horizontally(self):
        directorate_type = self.directorate_a.type
        department_type = self.department_a1.type
        secretary_general = self._node("الأمين العام", directorate_type, self.root)
        source_assistant = self._node("مساعد الأمين العام للشؤون الإدارية", directorate_type, secretary_general)
        target_assistant = self._node("مساعد الأمين العام للمنظمات والبرامج", directorate_type, secretary_general)
        source_general = self._node("الإدارة العامة للشؤون الإدارية", directorate_type, source_assistant)
        target_general = self._node("الإدارة العامة للدوائر المتخصصة", directorate_type, target_assistant)
        source_department = self._node("دائرة منشئ الطلب", department_type, source_general)
        target_department = self._node("دائرة الثقافة", department_type, target_general)
        second_target_department = self._node("دائرة التربية والتعليم العالي", department_type, target_general)

        secretary_manager = self._user("secretary@example.test", "مسؤول الأمين العام")
        source_assistant_manager = self._user("source-assistant@example.test", "مسؤول مساعد المصدر")
        source_general_manager = self._user("source-general@example.test", "مسؤول الإدارة العامة للمصدر")
        source_department_manager = self._user("source-department@example.test", "مسؤول دائرة المصدر")
        target_assistant_manager = self._user("target-assistant@example.test", "مسؤول مساعد الهدف")
        target_department_manager = self._user("culture@example.test", "مسؤول دائرة الثقافة")
        second_target_department_manager = self._user("education@example.test", "مسؤول دائرة التربية")
        self.requester.org_node_id = source_department.id
        db.session.add_all([
            OrgNodeManager(node_id=secretary_general.id, manager_user_id=secretary_manager.id),
            OrgNodeManager(node_id=source_assistant.id, manager_user_id=source_assistant_manager.id),
            OrgNodeManager(node_id=source_general.id, manager_user_id=source_general_manager.id),
            OrgNodeManager(node_id=source_department.id, manager_user_id=source_department_manager.id),
            OrgNodeManager(node_id=target_assistant.id, manager_user_id=target_assistant_manager.id),
            OrgNodeManager(node_id=target_general.id, manager_user_id=target_assistant_manager.id),
            OrgNodeManager(node_id=target_department.id, manager_user_id=target_department_manager.id),
            OrgNodeManager(node_id=second_target_department.id, manager_user_id=second_target_department_manager.id),
        ])
        db.session.commit()

        result = build_dynamic_target_path(self.requester, [
            f"NODE:{target_department.id}@{target_assistant.id}",
        ])

        self.assertEqual(result["errors"], [])
        self.assertEqual(
            [step["node_id"] for step in result["steps"]],
            [
                source_department.id,
                source_general.id,
                source_assistant.id,
                target_assistant.id,
                target_department.id,
            ],
        )
        self.assertEqual(
            [
                step["approver_user_id"]
                for step in result["steps"]
                if step["approver_kind"] == "USER"
            ].count(target_assistant_manager.id),
            1,
        )
        self.assertNotIn(self.root.id, [step["node_id"] for step in result["steps"]])
        self.assertNotIn(secretary_general.id, [step["node_id"] for step in result["steps"]])
        self.assertEqual(
            result["segments"][0]["target_ref"],
            f"NODE:{target_department.id}@{target_assistant.id}",
        )
        self.assertTrue(all(not step["reason"] for step in result["steps"]))

        with_secretary_general = build_dynamic_target_path(
            self.requester,
            [f"NODE:{target_department.id}@{target_assistant.id}"],
            include_secretary_general=True,
        )
        self.assertEqual(with_secretary_general["errors"], [])
        self.assertEqual(with_secretary_general["steps"][-1]["node_id"], secretary_general.id)
        self.assertEqual(with_secretary_general["steps"][-1]["approver_kind"], "ORG_NODE")
        self.assertEqual(with_secretary_general["steps"][-1]["reason"], "")

        sibling_result = build_dynamic_target_path(
            self.requester,
            [
                f"NODE:{target_department.id}@{target_assistant.id}",
                f"NODE:{second_target_department.id}@{target_assistant.id}",
            ],
        )
        self.assertEqual(sibling_result["errors"], [])
        self.assertEqual(
            [
                step["approver_org_node_id"]
                for step in sibling_result["steps"]
                if step["approver_kind"] == "ORG_NODE"
            ],
            [target_department.id, second_target_department.id],
        )
        self.assertEqual(
            [
                step["approver_user_id"]
                for step in sibling_result["steps"]
                if step["approver_kind"] == "USER"
            ].count(target_assistant_manager.id),
            1,
        )

    def test_scoped_node_route_rejects_a_start_within_the_top_two_levels(self):
        result = build_dynamic_target_path(self.requester, [
            f"NODE:{self.department_b.id}@{self.root.id}",
        ])

        self.assertTrue(result["errors"])
        self.assertIn("الجزء المسموح", " ".join(result["errors"]))

    def test_saved_dynamic_paths_are_scoped_to_their_owner(self):
        requester_preset = UserDynamicWorkflowPreset(
            user_id=self.requester.id,
            name="مساري المتكرر",
        )
        requester_preset.set_target_refs([
            f"NODE:{self.department_a1.id}",
            f"USER:{self.same_target.id}",
            FINAL_SECRETARY_GENERAL_REF,
        ])
        other_preset = UserDynamicWorkflowPreset(
            user_id=self.same_target.id,
            name="مساري المتكرر",
        )
        other_preset.set_target_refs([f"NODE:{self.department_b.id}"])
        db.session.add_all([requester_preset, other_preset])
        db.session.commit()

        requester_rows = UserDynamicWorkflowPreset.query.filter_by(
            user_id=self.requester.id,
        ).all()

        self.assertEqual(len(requester_rows), 1)
        self.assertEqual(requester_rows[0].name, "مساري المتكرر")
        self.assertEqual(requester_rows[0].target_refs(), [
            f"NODE:{self.department_a1.id}",
            f"USER:{self.same_target.id}",
            FINAL_SECRETARY_GENERAL_REF,
        ])
        self.assertEqual(len(self.requester.dynamic_workflow_presets.all()), 1)

    def test_primary_team_assignment_is_exposed_in_dynamic_choices(self):
        team_type = self._node_type("TEAM", "فريق", 40)
        team_node = self._node("فريق المتابعة", team_type, self.department_a2)
        db.session.add(OrgNodeAssignment(
            user_id=self.same_target.id,
            node_id=team_node.id,
            is_primary=True,
        ))
        db.session.commit()

        self.assertEqual(resolve_user_org_node_id(self.same_target), team_node.id)
        choice = next(
            item for item in dynamic_user_choices(self.requester)
            if item["id"] == self.same_target.id
        )
        self.assertEqual(choice["team_id"], team_node.id)
        self.assertEqual(choice["team_name"], "فريق المتابعة")
        self.assertIn("فريق المتابعة", choice["node_label"])

    def test_team_node_type_allows_optional_parent_or_root(self):
        team_type = self._node_type("TEAM", "فريق", 40)
        team_type.set_allowed_parent_type_ids([self.department_a1.type_id])
        db.session.commit()

        _tree, allowed_type_ids, _disabled_ids, root_allowed = _build_node_tree_for_picker(team_type)

        self.assertIn(self.department_a1.type_id, allowed_type_ids)
        self.assertTrue(root_allowed)

    def test_nested_general_administrations_under_same_top_manager_are_different(self):
        directorate_type = self.directorate_a.type
        department_type = self.department_a1.type
        secretary_general = self._node("الأمين العام", directorate_type, self.root)
        assistant_a = self._node("مساعد الأمين العام الأول", directorate_type, secretary_general)
        assistant_b = self._node("مساعد الأمين العام الثاني", directorate_type, secretary_general)
        general_a = self._node("الإدارة العامة للدوائر المتخصصة", directorate_type, assistant_a)
        general_b = self._node("الإدارة العامة للتخطيط والموارد البشرية", directorate_type, assistant_b)
        source_department = self._node("دائرة الاتصالات", department_type, general_a)
        target_department = self._node("دائرة الموارد البشرية", department_type, general_b)

        source_chain = node_chain(source_department.id)
        target_chain = node_chain(target_department.id)

        self.assertEqual(administration_anchor_id(source_chain), general_a.id)
        self.assertEqual(administration_anchor_id(target_chain), general_b.id)
        self.assertFalse(same_administration(source_chain, target_chain))
        self.assertTrue(same_administration(source_chain, node_chain(general_a.id)))

    def test_cross_administration_route_includes_configured_management_chain(self):
        result = build_dynamic_user_path(self.requester, [self.cross_target.id])

        self.assertEqual(result["errors"], [])
        user_ids = [step["approver_user_id"] for step in result["steps"]]
        self.assertEqual(user_ids, [
            self.source_manager.id,
            self.root_manager.id,
            self.target_manager.id,
            self.cross_target.id,
        ])
        self.assertFalse(result["segments"][0]["same_administration"])

    def test_cross_administration_route_is_rejected_without_any_manager(self):
        OrgNodeManager.query.delete()
        db.session.commit()

        result = build_dynamic_user_path(self.requester, [self.cross_target.id])

        self.assertTrue(result["errors"])
        self.assertIn("لم يتم تعيين مسؤول", " ".join(result["errors"]))

    def test_template_path_uses_only_nodes_with_assigned_managers(self):
        result = build_structural_template_path(self.department_a1.id, self.department_b.id)

        self.assertEqual(result["errors"], [])
        self.assertEqual(
            [step["approver_org_node_id"] for step in result["steps"]],
            [self.department_a1.id, self.root.id, self.department_b.id],
        )
        self.assertTrue(result["warnings"])

    def test_runtime_steps_start_without_saved_template(self):
        result = build_dynamic_user_path(self.requester, [self.same_target.id])
        request = WorkflowRequest(
            requester_id=self.requester.id,
            title="طلب ديناميكي",
            status="DRAFT",
            confidentiality="NORMAL",
        )
        db.session.add(request)
        db.session.flush()

        start_workflow_for_request(
            request,
            None,
            created_by_user_id=self.requester.id,
            runtime_steps=result["steps"],
            workflow_label="مسار ديناميكي حسب الهيكل الإداري",
        )
        db.session.commit()

        instance = WorkflowInstance.query.filter_by(request_id=request.id).one()
        step = WorkflowInstanceStep.query.filter_by(instance_id=instance.id).one()
        self.assertIsNone(instance.template_id)
        self.assertEqual(step.approver_kind, "USER")
        self.assertEqual(step.approver_user_id, self.same_target.id)
        self.assertEqual(step.routing_label, self.same_target.full_name)
        self.assertEqual(step.routing_node_label, result["steps"][0]["node_label"])
        self.assertEqual(step.routing_reason, result["steps"][0]["reason"])
        self.assertEqual(resolve_step_approver_user_ids(step), [self.same_target.id])
        self.assertIsNotNone(Notification.query.filter_by(user_id=self.same_target.id).first())

    def test_shared_manager_selects_one_sibling_branch_and_other_is_skipped(self):
        shared_manager = self._user("shared-manager@example.test", "مدير الإدارة العامة")
        db.session.add_all([
            OrgNodeManager(
                node_id=self.directorate_a.id,
                manager_user_id=shared_manager.id,
            ),
            OrgNodeManager(
                node_id=self.department_a2.id,
                manager_user_id=self.same_target.id,
            ),
        ])
        request = WorkflowRequest(
            requester_id=self.requester.id,
            title="اختيار دائرة واحدة",
            status="DRAFT",
            confidentiality="NORMAL",
        )
        db.session.add(request)
        db.session.flush()
        start_workflow_for_request(
            request,
            None,
            created_by_user_id=self.requester.id,
            runtime_steps=[
                {
                    "step_order": 1,
                    "mode": "SEQUENTIAL",
                    "approver_kind": "USER",
                    "approver_user_id": shared_manager.id,
                    "label": shared_manager.full_name,
                },
                {
                    "step_order": 2,
                    "mode": "SEQUENTIAL",
                    "approver_kind": "ORG_NODE",
                    "approver_org_node_id": self.department_a1.id,
                    "label": f"دائرة: {self.department_a1.name_ar}",
                },
                {
                    "step_order": 3,
                    "mode": "SEQUENTIAL",
                    "approver_kind": "ORG_NODE",
                    "approver_org_node_id": self.department_a2.id,
                    "label": f"دائرة: {self.department_a2.name_ar}",
                },
            ],
        )
        db.session.flush()

        instance = WorkflowInstance.query.filter_by(request_id=request.id).one()
        current_step = WorkflowInstanceStep.query.filter_by(
            instance_id=instance.id,
            step_order=1,
        ).one()
        self.assertEqual(
            [step.step_order for step in resolve_dynamic_branch_steps(instance, current_step)],
            [2, 3],
        )

        with self.assertRaisesRegex(ValueError, "اختر دائرة واحدة"):
            decide_step(
                request.id,
                1,
                shared_manager.id,
                "APPROVED",
                auto_commit=False,
            )

        decide_step(
            request.id,
            1,
            shared_manager.id,
            "APPROVED",
            auto_commit=False,
            selected_dynamic_branch_step_order=3,
        )
        db.session.flush()

        first_branch = WorkflowInstanceStep.query.filter_by(
            instance_id=instance.id,
            step_order=2,
        ).one()
        selected_branch = WorkflowInstanceStep.query.filter_by(
            instance_id=instance.id,
            step_order=3,
        ).one()
        self.assertEqual(first_branch.status, "SKIPPED")
        self.assertEqual(selected_branch.status, "PENDING")
        self.assertEqual(instance.current_step_order, 3)
        self.assertIsNotNone(AuditLog.query.filter_by(
            request_id=request.id,
            action="DYNAMIC_BRANCH_SELECTED",
        ).first())

        decide_step(
            request.id,
            3,
            self.same_target.id,
            "APPROVED",
            auto_commit=False,
        )
        db.session.flush()
        self.assertTrue(instance.is_completed)
        self.assertEqual(request.status, "APPROVED")

    def test_team_can_be_created_without_hierarchy_parent(self):
        team = Team(name_ar="فريق مستقل", section_id=None, division_id=None, is_active=True)
        db.session.add(team)
        db.session.commit()

        saved = Team.query.get(team.id)
        self.assertIsNotNone(saved)
        self.assertIsNone(saved.section_id)
        self.assertIsNone(saved.division_id)

    def test_independent_team_membership_syncs_to_dynamic_choices(self):
        team = Team(name_ar="فريق الميدان", section_id=None, division_id=None, is_active=True)
        db.session.add(team)
        db.session.flush()
        db.session.add(OrgUnitAssignment(
            user_id=self.same_target.id,
            unit_type="TEAM",
            unit_id=team.id,
            is_primary=True,
            title="عضو فريق",
        ))
        db.session.commit()

        sync_legacy_now()

        team_node = OrgNode.query.filter_by(legacy_type="TEAM", legacy_id=team.id).one()
        self.assertIsNone(team_node.parent_id)
        self.assertEqual(resolve_user_org_node_id(self.same_target), self.department_a2.id)
        self.assertIsNotNone(TeamMembership.query.filter_by(
            team_id=team.id,
            user_id=self.same_target.id,
        ).first())
        choice = next(
            item for item in dynamic_user_choices(self.requester)
            if item["id"] == self.same_target.id
        )
        self.assertEqual(choice["team_name"], "فريق الميدان")

    def test_employee_can_join_multiple_teams_without_changing_org_node(self):
        first_team = Team(name_ar="فريق أول", is_active=True)
        second_team = Team(name_ar="فريق ثان", is_active=True)
        db.session.add_all([first_team, second_team])
        db.session.flush()
        db.session.add_all([
            TeamMembership(team_id=first_team.id, user_id=self.same_target.id, is_active=True),
            TeamMembership(team_id=second_team.id, user_id=self.same_target.id, is_active=True),
        ])
        db.session.commit()

        self.assertEqual(resolve_user_org_node_id(self.same_target), self.department_a2.id)
        choice = next(
            item for item in dynamic_user_choices(self.requester)
            if item["id"] == self.same_target.id
        )
        self.assertEqual(set(choice["team_names"]), {"فريق أول", "فريق ثان"})
        self.assertEqual(len(choice["team_keys"]), 2)

    def test_saved_org_node_step_is_copied_and_notified(self):
        template = WorkflowTemplate(name="مسار هيكلي محفوظ", created_by_id=self.requester.id)
        db.session.add(template)
        db.session.flush()
        db.session.add(WorkflowTemplateStep(
            template_id=template.id,
            step_order=1,
            mode="SEQUENTIAL",
            approver_kind="ORG_NODE",
            approver_org_node_id=self.department_b.id,
        ))
        request = WorkflowRequest(
            requester_id=self.requester.id,
            title="طلب بقالب هيكلي",
            status="DRAFT",
            confidentiality="NORMAL",
        )
        db.session.add(request)
        db.session.flush()

        start_workflow_for_request(request, template, created_by_user_id=self.requester.id)
        db.session.commit()

        instance = WorkflowInstance.query.filter_by(request_id=request.id).one()
        step = WorkflowInstanceStep.query.filter_by(instance_id=instance.id).one()
        self.assertEqual(step.approver_kind, "ORG_NODE")
        self.assertEqual(step.approver_org_node_id, self.department_b.id)
        self.assertIsNotNone(Notification.query.filter_by(user_id=self.target_manager.id).first())

    def test_structural_route_traverses_common_parent_once(self):
        route = structural_route_nodes(self.department_a1.id, self.department_b.id)
        route_ids = [node.id for node in route]
        self.assertEqual(route_ids.count(self.root.id), 1)
        self.assertEqual(route_ids[0], self.department_a1.id)
        self.assertEqual(route_ids[-1], self.department_b.id)


if __name__ == "__main__":
    unittest.main()
