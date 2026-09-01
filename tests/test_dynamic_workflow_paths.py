import unittest
from io import BytesIO
from unittest.mock import patch

from flask import Flask
from PyPDF2 import PdfReader
from werkzeug.exceptions import Forbidden

from extensions import db
from models import (
    AuditLog,
    Committee,
    CommitteeAssignee,
    Department,
    Directorate,
    Notification,
    OrgNode,
    OrgNodeAssignment,
    OrgNodeManager,
    OrgNodeType,
    OrgUnitAssignment,
    OrgUnitManager,
    Organization,
    RequestEscalation,
    RequestType,
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
from workflow.routes import (
    _find_or_create_request_type,
    _hierarchy_manager_user_ids,
    _user_can_view_request,
    close_request,
    escalate_request,
    request_pdf,
)
from workflow.dynamic_paths import (
    DYNAMIC_RETURN_REASON,
    FINAL_SECRETARY_GENERAL_REF,
    administration_anchor_id,
    build_dynamic_target_path,
    build_dynamic_user_path,
    build_structural_template_path,
    dynamic_committee_choices,
    dynamic_org_browser_nodes,
    dynamic_user_choices,
    hierarchy_position_label,
    node_chain,
    node_path_label,
    requester_dynamic_manager_options,
    same_administration,
    structural_route_nodes,
)
from workflow.engine import (
    decide_step,
    resolve_dynamic_branch_steps,
    resolve_hierarchy_bypass_step,
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

    def test_same_administration_user_still_follows_the_vertical_manager_route(self):
        result = build_dynamic_user_path(self.requester, [self.same_target.id])

        self.assertEqual(result["errors"], [])
        self.assertEqual(
            [step["approver_user_id"] for step in result["steps"]],
            [self.source_manager.id, self.same_target.id],
        )
        self.assertEqual(result["origin"]["user_id"], self.requester.id)
        self.assertTrue(all("عمودي" in step["reason"] for step in result["steps"]))

    def test_non_managerial_peers_are_not_repeated_on_return(self):
        directorate_manager = self._user("directorate-manager@example.test", "مدير الإدارة")
        first_peer = self._user("first-peer@example.test", "دعاء")
        second_peer = self._user("second-peer@example.test", "مرح")
        directorate_manager.org_node_id = self.directorate_a.id
        first_peer.org_node_id = self.department_a2.id
        second_peer.org_node_id = self.department_a2.id
        db.session.add(OrgNodeManager(
            node_id=self.directorate_a.id,
            manager_user_id=directorate_manager.id,
        ))
        db.session.commit()

        result = build_dynamic_target_path(
            self.requester,
            [f"USER:{first_peer.id}", f"USER:{second_peer.id}"],
        )

        self.assertEqual(result["errors"], [])
        self.assertEqual(
            [step.get("approver_user_id") for step in result["steps"]],
            [
                self.source_manager.id,
                directorate_manager.id,
                first_peer.id,
                second_peer.id,
                directorate_manager.id,
                self.source_manager.id,
            ],
        )
        self.assertEqual(
            [step["reason"] for step in result["steps"][-2:]],
            [DYNAMIC_RETURN_REASON, DYNAMIC_RETURN_REASON],
        )

    def test_manager_is_repeated_on_return_even_at_the_same_level(self):
        directorate_manager = self._user("directorate-manager@example.test", "مدير الإدارة")
        manager = self._user("peer-manager@example.test", "دعاء")
        employee = self._user("peer-employee@example.test", "مرح")
        directorate_manager.org_node_id = self.directorate_a.id
        manager.org_node_id = self.department_a2.id
        employee.org_node_id = self.department_a2.id
        db.session.add_all((
            OrgNodeManager(
                node_id=self.directorate_a.id,
                manager_user_id=directorate_manager.id,
            ),
            OrgNodeManager(
                node_id=self.department_a2.id,
                manager_user_id=manager.id,
            ),
        ))
        db.session.commit()

        result = build_dynamic_target_path(
            self.requester,
            [f"USER:{manager.id}", f"USER:{employee.id}"],
        )

        self.assertEqual(result["errors"], [])
        self.assertEqual(
            [step.get("approver_user_id") for step in result["steps"]],
            [
                self.source_manager.id,
                directorate_manager.id,
                manager.id,
                employee.id,
                manager.id,
                directorate_manager.id,
                self.source_manager.id,
            ],
        )

    def test_user_entered_request_type_is_created_and_reused_by_label(self):
        created = _find_or_create_request_type("  طلب   متابعة خاص  ")
        reused = _find_or_create_request_type("طلب متابعة خاص")

        self.assertEqual(created.id, reused.id)
        self.assertEqual(created.name_ar, "طلب متابعة خاص")
        self.assertTrue(created.code.startswith("USR_"))
        self.assertTrue(created.is_active)
        self.assertEqual(RequestType.query.count(), 1)

    def test_workflow_request_priority_defaults_to_normal(self):
        request_row = WorkflowRequest(
            requester_id=self.requester.id,
            title="طلب بأولوية اختيارية",
            status="DRAFT",
        )
        db.session.add(request_row)
        db.session.flush()

        self.assertEqual(request_row.priority, "NORMAL")

    def test_request_creator_can_close_request_after_final_decision(self):
        request_row = WorkflowRequest(
            requester_id=self.requester.id,
            title="طلب مكتمل بانتظار الإغلاق",
            status="APPROVED",
        )
        db.session.add(request_row)
        db.session.flush()
        db.session.add(WorkflowInstance(
            request_id=request_row.id,
            template_id=None,
            current_step_order=1,
            is_completed=True,
        ))
        db.session.commit()

        with self.app.test_request_context(method="POST"):
            with patch("workflow.routes.current_user", self.requester), patch(
                "workflow.routes.url_for", return_value=f"/workflow/request/{request_row.id}"
            ):
                response = close_request.__wrapped__(request_row.id)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.session.get(WorkflowRequest, request_row.id).status, "CLOSED")
        audit = AuditLog.query.filter_by(
            request_id=request_row.id,
            action="REQUEST_CLOSED",
        ).one()
        self.assertEqual(audit.user_id, self.requester.id)
        self.assertEqual(audit.old_status, "APPROVED")
        self.assertEqual(audit.new_status, "CLOSED")

    def test_non_creator_cannot_close_request(self):
        request_row = WorkflowRequest(
            requester_id=self.requester.id,
            title="طلب لا يغلقه غير منشئه",
            status="APPROVED",
        )
        db.session.add(request_row)
        db.session.commit()

        with self.app.test_request_context(method="POST"):
            with patch("workflow.routes.current_user", self.same_target):
                with self.assertRaises(Forbidden):
                    close_request.__wrapped__(request_row.id)

        self.assertEqual(db.session.get(WorkflowRequest, request_row.id).status, "APPROVED")

    def test_creator_cannot_close_request_before_workflow_finishes(self):
        request_row = WorkflowRequest(
            requester_id=self.requester.id,
            title="طلب ما زال قيد التنفيذ",
            status="IN_PROGRESS",
        )
        db.session.add(request_row)
        db.session.commit()

        with self.app.test_request_context(method="POST"):
            with patch("workflow.routes.current_user", self.requester), patch(
                "workflow.routes.url_for", return_value=f"/workflow/request/{request_row.id}"
            ):
                response = close_request.__wrapped__(request_row.id)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.session.get(WorkflowRequest, request_row.id).status, "IN_PROGRESS")
        self.assertIsNone(AuditLog.query.filter_by(
            request_id=request_row.id,
            action="REQUEST_CLOSED",
        ).first())

    def test_request_pdf_is_a_short_inline_report(self):
        request_type = _find_or_create_request_type("طلب متابعة")
        request_row = WorkflowRequest(
            requester_id=self.requester.id,
            request_type_id=request_type.id,
            title="طلب تجربة الطباعة",
            description="وصف مفيد فقط",
            status="DRAFT",
        )
        db.session.add(request_row)
        db.session.flush()
        start_workflow_for_request(
            request_row,
            None,
            created_by_user_id=self.requester.id,
            runtime_steps=[{
                "approver_kind": "USER",
                "approver_user_id": self.same_target.id,
                "label": self.same_target.full_name,
                "job_title": "موظف متابعة",
                "reason": "مراجعة الطلب وإضافة الملاحظة",
            }],
            workflow_label="مسار ديناميكي حسب الهيكلية",
        )
        db.session.commit()

        with self.app.test_request_context():
            with patch("workflow.routes.current_user", self.requester), patch(
                "workflow.routes._user_can_view_request", return_value=True
            ):
                response = request_pdf.__wrapped__(request_row.id)
                response.direct_passthrough = False
                payload = response.get_data()

        self.assertTrue(payload.startswith(b"%PDF"))
        self.assertEqual(len(PdfReader(BytesIO(payload)).pages), 1)
        self.assertIn("inline", response.headers.get("Content-Disposition", ""))

    def test_dynamic_org_browser_exposes_all_active_entities_and_manager_context(self):
        empty_department = self._node("دائرة بلا موظفين", self.department_a1.type, self.directorate_a)
        db.session.commit()

        nodes = dynamic_org_browser_nodes(dynamic_user_choices(self.requester), self.requester)
        nodes_by_id = {node["id"]: node for node in nodes}

        self.assertTrue(nodes)
        self.assertTrue(all((node.get("name") or "").strip() for node in nodes))
        self.assertTrue(all(
            node.get("parent_id") is None or node["parent_id"] in nodes_by_id
            for node in nodes
        ))
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
        self.assertNotIn("route_start_options", nodes_by_id[self.department_a1.id])
        self.assertFalse(nodes_by_id[empty_department.id]["can_select"])
        self.assertGreaterEqual(
            nodes_by_id[self.directorate_a.id]["total_user_count"],
            nodes_by_id[self.department_a2.id]["direct_user_count"],
        )

    def test_dynamic_committee_choices_expose_only_available_delivery_modes(self):
        committee = Committee(name_ar="لجنة الاختبار", code="TEST", is_active=True)
        inactive_committee = Committee(name_ar="لجنة غير مفعلة", is_active=False)
        db.session.add_all([committee, inactive_committee])
        db.session.flush()
        db.session.add_all([
            CommitteeAssignee(
                committee_id=committee.id,
                kind="USER",
                user_id=self.same_target.id,
                member_role="CHAIR",
                is_active=True,
            ),
            CommitteeAssignee(
                committee_id=committee.id,
                kind="USER",
                user_id=self.cross_target.id,
                member_role="MEMBER",
                is_active=True,
            ),
        ])
        db.session.commit()

        choices = dynamic_committee_choices()

        self.assertEqual([choice["id"] for choice in choices], [committee.id])
        self.assertTrue(choices[0]["can_select"])
        self.assertEqual(choices[0]["member_count"], 2)
        self.assertEqual(
            [mode["key"] for mode in choices[0]["available_modes"]],
            ["ALL", "CHAIR"],
        )

    def test_dynamic_path_can_end_at_committee_and_runtime_resolves_its_chair(self):
        committee = Committee(name_ar="لجنة المسار الديناميكي", is_active=True)
        db.session.add(committee)
        db.session.flush()
        db.session.add_all([
            CommitteeAssignee(
                committee_id=committee.id,
                kind="USER",
                user_id=self.same_target.id,
                member_role="CHAIR",
                is_active=True,
            ),
            CommitteeAssignee(
                committee_id=committee.id,
                kind="USER",
                user_id=self.cross_target.id,
                member_role="MEMBER",
                is_active=True,
            ),
        ])
        db.session.commit()

        result = build_dynamic_target_path(
            self.requester,
            [f"COMMITTEE:{committee.id}@CHAIR"],
        )

        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["steps"]), 1)
        self.assertEqual(result["steps"][0]["approver_kind"], "COMMITTEE")
        self.assertEqual(result["steps"][0]["approver_committee_id"], committee.id)
        self.assertEqual(result["steps"][0]["committee_delivery_mode"], "Committee_CHAIR")
        self.assertEqual(
            result["segments"][0]["target_ref"],
            f"COMMITTEE:{committee.id}@CHAIR",
        )
        unlinked_requester = self._user("unlinked@example.test", "موظف غير مربوط")
        db.session.commit()
        unlinked_result = build_dynamic_target_path(
            unlinked_requester,
            [f"COMMITTEE:{committee.id}@ALL"],
        )
        self.assertEqual(unlinked_result["errors"], [])

        request_row = WorkflowRequest(
            requester_id=self.requester.id,
            title="طلب إلى لجنة",
            status="DRAFT",
            confidentiality="NORMAL",
        )
        db.session.add(request_row)
        db.session.flush()
        start_workflow_for_request(
            request_row,
            None,
            created_by_user_id=self.requester.id,
            runtime_steps=result["steps"],
            workflow_label="مسار ديناميكي إلى لجنة",
        )
        db.session.commit()

        instance = WorkflowInstance.query.filter_by(request_id=request_row.id).one()
        step = WorkflowInstanceStep.query.filter_by(instance_id=instance.id).one()
        self.assertEqual(step.approver_kind, "COMMITTEE")
        self.assertEqual(step.approver_committee_id, committee.id)
        self.assertEqual(step.committee_delivery_mode, "Committee_CHAIR")
        self.assertEqual(resolve_step_approver_user_ids(step), [self.same_target.id])
        self.assertIsNotNone(Notification.query.filter_by(user_id=self.same_target.id).first())
        self.assertIsNone(Notification.query.filter_by(user_id=self.cross_target.id).first())

    def test_dynamic_committee_must_be_last_and_have_the_requested_member_role(self):
        committee = Committee(name_ar="لجنة بلا مقرر", is_active=True)
        db.session.add(committee)
        db.session.flush()
        db.session.add(CommitteeAssignee(
            committee_id=committee.id,
            kind="USER",
            user_id=self.same_target.id,
            member_role="CHAIR",
            is_active=True,
        ))
        db.session.commit()

        not_last = build_dynamic_target_path(self.requester, [
            f"COMMITTEE:{committee.id}@ALL",
            f"USER:{self.same_target.id}",
        ])
        missing_secretary = build_dynamic_target_path(
            self.requester,
            [f"COMMITTEE:{committee.id}@SECRETARY"],
        )

        self.assertIn("آخر وجهة", " ".join(not_last["errors"]))
        self.assertIn("مقرر اللجنة", " ".join(missing_secretary["errors"]))

    def test_hierarchy_position_uses_the_workflow_step_node(self):
        second_managed_node = self._node(
            "دائرة إضافية",
            self.department_a1.type,
            self.directorate_a,
        )
        general_administration = self._node(
            "الإدارة العامة للبرامج والمشاريع",
            self.directorate_a.type,
            self.root,
        )
        assistant_secretary = self._node(
            "مساعد الأمين العام للمنظمات والبرامج والمشاريع",
            self.directorate_a.type,
            self.root,
        )
        db.session.add(OrgNodeManager(
            node_id=second_managed_node.id,
            deputy_user_id=self.source_manager.id,
        ))
        db.session.add_all([
            OrgNodeManager(
                node_id=general_administration.id,
                manager_user_id=self.source_manager.id,
            ),
            OrgNodeManager(
                node_id=assistant_secretary.id,
                manager_user_id=self.source_manager.id,
            ),
        ])
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
        self.assertEqual(
            hierarchy_position_label(
                self.source_manager,
                routing_node_label=node_path_label(general_administration),
            ),
            "مدير عام الإدارة العامة للبرامج والمشاريع",
        )
        self.assertEqual(
            hierarchy_position_label(
                self.source_manager,
                routing_node_label=node_path_label(assistant_secretary),
            ),
            "مساعد الأمين العام للمنظمات والبرامج والمشاريع",
        )

        db.session.add(OrgNodeAssignment(
            user_id=self.source_manager.id,
            node_id=second_managed_node.id,
            is_primary=False,
            title="القائم بأعمال مدير الدائرة",
        ))
        db.session.commit()
        self.assertEqual(
            hierarchy_position_label(
                self.source_manager,
                routing_node_label=node_path_label(second_managed_node),
            ),
            "القائم بأعمال مدير الدائرة",
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

    def test_requester_can_choose_managers_from_all_org_assignments(self):
        legacy_organization = Organization(name_ar="مؤسسة التعيينات", code="ASSIGNMENTS")
        db.session.add(legacy_organization)
        db.session.flush()
        legacy_directorate = Directorate(
            organization_id=legacy_organization.id,
            name_ar="إدارة التعيينات الإضافية",
        )
        db.session.add(legacy_directorate)
        db.session.flush()
        legacy_department_irene = Department(
            directorate_id=legacy_directorate.id,
            name_ar="دائرة المعلومات",
        )
        legacy_department_raed = Department(
            directorate_id=legacy_directorate.id,
            name_ar="دائرة المشاريع",
        )
        db.session.add_all([legacy_department_irene, legacy_department_raed])
        db.session.flush()

        irene = self._user("irene@example.test", "إيرين")
        raed = self._user("raed@example.test", "رائد")
        db.session.add_all([
            # These managers intentionally exist only in the legacy HR
            # hierarchy. This mirrors approved/locked databases where
            # secondary assignments were not copied into OrgNodeManager.
            OrgUnitManager(
                unit_type="DEPARTMENT",
                unit_id=legacy_department_irene.id,
                manager_user_id=irene.id,
            ),
            OrgUnitManager(
                unit_type="DEPARTMENT",
                unit_id=legacy_department_raed.id,
                manager_user_id=raed.id,
            ),
            OrgUnitAssignment(
                user_id=self.requester.id,
                unit_type="DEPARTMENT",
                unit_id=legacy_department_irene.id,
                is_primary=False,
            ),
            OrgUnitAssignment(
                user_id=self.requester.id,
                unit_type="DEPARTMENT",
                unit_id=legacy_department_raed.id,
                is_primary=False,
            ),
        ])
        db.session.commit()

        options = requester_dynamic_manager_options(self.requester)
        self.assertEqual(options[0]["user_id"], self.source_manager.id)
        self.assertEqual(
            {option["user_id"] for option in options},
            {self.source_manager.id, irene.id, raed.id},
        )

        selected_result = build_dynamic_target_path(
            self.requester,
            [f"NODE:{self.department_b.id}"],
            selected_manager_user_ids=[irene.id],
        )
        self.assertEqual(selected_result["errors"], [])
        self.assertEqual(selected_result["steps"][0]["approver_user_id"], irene.id)
        self.assertEqual(
            selected_result["steps"][1]["approver_org_node_id"],
            self.department_b.id,
        )
        self.assertEqual(selected_result["steps"][2]["approver_user_id"], irene.id)
        self.assertNotIn(
            self.source_manager.id,
            [step.get("approver_user_id") for step in selected_result["steps"]],
        )

        all_result = build_dynamic_target_path(
            self.requester,
            [f"NODE:{self.department_b.id}"],
            selected_manager_user_ids=[self.source_manager.id, irene.id, raed.id],
        )
        self.assertEqual(all_result["errors"], [])
        self.assertEqual(
            [step.get("approver_user_id") for step in all_result["steps"][:3]],
            [self.source_manager.id, irene.id, raed.id],
        )

        empty_result = build_dynamic_target_path(
            self.requester,
            [f"NODE:{self.department_b.id}"],
            selected_manager_user_ids=[],
        )
        self.assertEqual(empty_result["errors"], [])
        self.assertEqual(empty_result["selected_manager_user_ids"], [])
        self.assertTrue(
            all(
                step.get("approver_user_id") not in {self.source_manager.id, irene.id, raed.id}
                for step in empty_result["steps"]
            )
        )

    def test_requester_includes_every_non_governance_hierarchy_manager(self):
        directorate_manager = self._user(
            "directorate-manager@example.test",
            "مدير الإدارة",
        )
        directorate_deputy = self._user(
            "directorate-deputy@example.test",
            "نائب مدير الإدارة",
        )
        db.session.add(OrgNodeManager(
            node_id=self.directorate_a.id,
            manager_user_id=directorate_manager.id,
            deputy_user_id=directorate_deputy.id,
        ))
        db.session.commit()

        options = requester_dynamic_manager_options(self.requester)

        self.assertEqual(
            {option["user_id"] for option in options},
            {
                self.source_manager.id,
                directorate_manager.id,
                directorate_deputy.id,
            },
        )
        self.assertNotIn(
            self.root_manager.id,
            {option["user_id"] for option in options},
        )

    def test_requester_managerial_appointments_expand_manager_choices(self):
        programs_manager = self._user(
            "programs-manager@example.test",
            "مدير البرامج",
        )
        information_manager = self._user(
            "information-manager@example.test",
            "مدير المعلومات",
        )
        programs_node = self._node(
            "إدارة البرامج",
            self.directorate_a.type,
            self.root,
        )
        requester_programs_node = self._node(
            "دائرة البرامج",
            self.department_a1.type,
            self.directorate_a,
        )
        requester_information_node = self._node(
            "دائرة المعلومات",
            self.department_a1.type,
            programs_node,
        )
        db.session.add_all((
            OrgNodeManager(
                node_id=self.directorate_a.id,
                manager_user_id=programs_manager.id,
            ),
            OrgNodeManager(
                node_id=programs_node.id,
                manager_user_id=information_manager.id,
            ),
            OrgNodeManager(
                node_id=requester_programs_node.id,
                manager_user_id=self.requester.id,
            ),
            OrgNodeManager(
                node_id=requester_information_node.id,
                deputy_user_id=self.requester.id,
            ),
        ))
        db.session.commit()

        options = requester_dynamic_manager_options(self.requester)

        self.assertEqual(
            {option["user_id"] for option in options},
            {
                self.source_manager.id,
                programs_manager.id,
                information_manager.id,
            },
        )

    def test_node_route_is_automatic_vertical_and_excludes_top_governance_levels(self):
        directorate_type = self.directorate_a.type
        department_type = self.department_a1.type
        chairperson = self._node("رئيس اللجنة", directorate_type, self.root)
        secretary_general = self._node("الأمين العام", directorate_type, chairperson)
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

        browser_nodes = dynamic_org_browser_nodes(
            dynamic_user_choices(self.requester),
            self.requester,
        )
        target_browser = next(
            node for node in browser_nodes if node["id"] == target_department.id
        )
        self.assertTrue(target_browser["can_select"])
        self.assertNotIn("route_start_options", target_browser)

        legacy_ref_result = build_dynamic_target_path(self.requester, [
            f"NODE:{target_department.id}@{secretary_general.id}",
        ])
        self.assertEqual(legacy_ref_result["errors"], [])
        self.assertEqual(
            legacy_ref_result["segments"][0]["target_ref"],
            f"NODE:{target_department.id}",
        )

        result = build_dynamic_target_path(self.requester, [
            f"NODE:{target_department.id}",
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
                target_assistant.id,
                source_general.id,
                source_department.id,
            ],
        )
        self.assertEqual(
            [
                step["approver_user_id"]
                for step in result["steps"]
                if step["approver_kind"] == "USER"
            ].count(target_assistant_manager.id),
            2,
        )
        self.assertNotIn(self.root.id, [step["node_id"] for step in result["steps"]])
        self.assertNotIn(secretary_general.id, [step["node_id"] for step in result["steps"]])
        self.assertEqual(
            result["segments"][0]["target_ref"],
            f"NODE:{target_department.id}",
        )
        self.assertTrue(all("عمودي" in step["reason"] for step in result["steps"][:4]))
        self.assertEqual(result["origin"]["user_id"], self.requester.id)
        self.assertTrue(all(
            step["reason"] == DYNAMIC_RETURN_REASON
            for step in result["steps"][5:]
        ))

        with_secretary_general = build_dynamic_target_path(
            self.requester,
            [f"NODE:{target_department.id}"],
            include_secretary_general=True,
        )
        self.assertEqual(with_secretary_general["errors"], [])
        secretary_step = with_secretary_general["steps"][len(with_secretary_general["steps"]) // 2]
        self.assertEqual(secretary_step["node_id"], secretary_general.id)
        self.assertEqual(secretary_step["approver_kind"], "ORG_NODE")
        self.assertEqual(secretary_step["label"], secretary_manager.full_name)
        self.assertEqual(secretary_step["reason"], "")
        self.assertEqual(with_secretary_general["steps"][-1]["node_id"], source_department.id)
        self.assertEqual(with_secretary_general["steps"][-1]["reason"], DYNAMIC_RETURN_REASON)

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
            2,
        )

    def test_managerless_common_level_is_not_repeated_on_return(self):
        directorate_type = self.directorate_a.type
        department_type = self.department_a1.type
        chairperson = self._node("رئيس اللجنة للمسار", directorate_type, self.root)
        secretary_general = self._node("الأمين العام", directorate_type, chairperson)
        shared_assistant = self._node(
            "مساعد الأمين العام للثقافة والبرامج",
            directorate_type,
            secretary_general,
        )
        source_general = self._node(
            "الإدارة العامة للثقافة",
            directorate_type,
            shared_assistant,
        )
        target_general = self._node(
            "الإدارة العامة للبرامج والمشاريع",
            directorate_type,
            shared_assistant,
        )
        source_department = self._node("دائرة الثقافة", department_type, source_general)
        target_department = self._node("دائرة البرامج", department_type, target_general)

        kholoud = self._user("kholoud-route@example.test", "خلود")
        irene = self._user("irene-route@example.test", "إيرين")
        adham = self._user("adham-route@example.test", "أدهم")
        self.requester.name = "أيمن"
        self.requester.org_node_id = source_department.id
        db.session.add_all([
            OrgNodeManager(
                node_id=source_general.id,
                manager_user_id=kholoud.id,
            ),
            OrgNodeManager(
                node_id=target_general.id,
                manager_user_id=irene.id,
            ),
            OrgNodeManager(
                node_id=target_department.id,
                manager_user_id=adham.id,
            ),
        ])
        db.session.commit()

        browser_nodes = dynamic_org_browser_nodes(
            dynamic_user_choices(self.requester),
            self.requester,
        )
        target_browser = next(
            node for node in browser_nodes if node["id"] == target_department.id
        )
        self.assertTrue(target_browser["can_select"])
        self.assertNotIn("route_start_options", target_browser)

        result = build_dynamic_target_path(self.requester, [
            f"NODE:{target_department.id}",
        ])

        self.assertEqual(result["errors"], [])
        self.assertEqual(
            [result["origin"]["label"]] + [step["label"] for step in result["steps"][:3]],
            ["أيمن", "خلود", "إيرين", "أدهم"],
        )
        self.assertEqual(
            [step.get("approver_user_id") for step in result["steps"][:3]],
            [kholoud.id, irene.id, None],
        )
        self.assertEqual(
            [step["node_id"] for step in result["steps"]],
            [
                source_general.id,
                target_general.id,
                target_department.id,
                target_general.id,
            ],
        )
        self.assertIn(shared_assistant.name_ar, " ".join(result["warnings"]))

    def test_legacy_node_route_start_suffix_is_ignored_and_normalized(self):
        result = build_dynamic_target_path(self.requester, [
            f"NODE:{self.department_b.id}@{self.root.id}",
        ])

        self.assertEqual(result["errors"], [])
        self.assertEqual(
            result["segments"][0]["target_ref"],
            f"NODE:{self.department_b.id}",
        )

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
        steps = WorkflowInstanceStep.query.filter_by(
            instance_id=instance.id,
        ).order_by(WorkflowInstanceStep.step_order.asc()).all()
        self.assertIsNone(instance.template_id)
        self.assertEqual(
            [step.approver_user_id for step in steps],
            [self.source_manager.id, self.same_target.id],
        )
        self.assertEqual(steps[0].routing_label, self.source_manager.full_name)
        self.assertEqual(steps[0].routing_node_label, result["steps"][0]["node_label"])
        self.assertEqual(steps[0].routing_reason, result["steps"][0]["reason"])
        self.assertEqual(resolve_step_approver_user_ids(steps[0]), [self.source_manager.id])
        self.assertIsNotNone(Notification.query.filter_by(user_id=self.source_manager.id).first())

    def test_higher_dynamic_approver_bypasses_lower_and_keeps_them_following(self):
        db.session.add(OrgNodeManager(
            node_id=self.directorate_a.id,
            manager_user_id=self.root_manager.id,
        ))
        db.session.commit()
        result = build_dynamic_target_path(
            self.requester,
            [f"NODE:{self.department_b.id}"],
        )
        self.assertEqual(result["errors"], [])
        self.assertEqual(
            [step["node_id"] for step in result["steps"]],
            [
                self.department_a1.id,
                self.directorate_a.id,
                self.department_b.id,
                self.directorate_a.id,
                self.department_a1.id,
            ],
        )

        request = WorkflowRequest(
            requester_id=self.requester.id,
            title="طلب تجاوز هرمي",
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
        )
        db.session.commit()

        instance = WorkflowInstance.query.filter_by(request_id=request.id).one()
        self.assertIsNone(resolve_hierarchy_bypass_step(instance, [self.target_manager.id]))
        with self.assertRaisesRegex(ValueError, "ليست مستوى أعلى"):
            decide_step(
                request.id,
                3,
                self.target_manager.id,
                "APPROVED",
                auto_commit=False,
            )
        db.session.rollback()

        bypass_step = resolve_hierarchy_bypass_step(instance, [self.root_manager.id])
        self.assertIsNotNone(bypass_step)
        self.assertEqual(bypass_step.step_order, 2)

        decide_step(
            request.id,
            bypass_step.step_order,
            self.root_manager.id,
            "APPROVED",
            note="متابعة من المستوى الأعلى",
            auto_commit=False,
        )
        db.session.commit()

        lower_step = WorkflowInstanceStep.query.filter_by(
            instance_id=instance.id,
            step_order=1,
        ).one()
        self.assertEqual(lower_step.status, "SKIPPED")
        self.assertEqual(bypass_step.status, "APPROVED")
        self.assertEqual(instance.current_step_order, 3)
        self.assertIsNotNone(AuditLog.query.filter_by(
            request_id=request.id,
            action="HIERARCHY_BYPASS_FOLLOWER",
            target_type="USER",
            target_id=self.source_manager.id,
        ).first())
        self.assertTrue(_user_can_view_request(self.source_manager, request))
        self.assertTrue(any(
            "ستبقى مطلعاً" in notification.message
            for notification in Notification.query.filter_by(
                user_id=self.source_manager.id,
            ).all()
        ))

        decide_step(
            request.id,
            3,
            self.target_manager.id,
            "APPROVED",
            auto_commit=False,
        )
        db.session.commit()

        self.assertEqual(instance.current_step_order, 4)
        self.assertTrue(any(
            "تحديث على المسار" in notification.message
            for notification in Notification.query.filter_by(
                user_id=self.source_manager.id,
            ).all()
        ))

        decide_step(
            request.id,
            4,
            self.root_manager.id,
            "APPROVED",
            auto_commit=False,
        )
        db.session.flush()
        self.assertEqual(instance.current_step_order, 5)

        decide_step(
            request.id,
            5,
            self.source_manager.id,
            "APPROVED",
            auto_commit=False,
        )
        db.session.commit()
        self.assertTrue(instance.is_completed)
        self.assertEqual(request.status, "APPROVED")

    def test_shared_manager_selects_multiple_sibling_branches_and_others_are_skipped(self):
        shared_manager = self._user("shared-manager@example.test", "مدير الإدارة العامة")
        third_branch_node = self._node(
            "دائرة ثالثة",
            self.department_a1.type,
            self.directorate_a,
        )
        db.session.add_all([
            OrgNodeManager(
                node_id=self.directorate_a.id,
                manager_user_id=shared_manager.id,
            ),
            OrgNodeManager(
                node_id=self.department_a2.id,
                manager_user_id=self.same_target.id,
            ),
            OrgNodeManager(
                node_id=third_branch_node.id,
                manager_user_id=self.cross_target.id,
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
                {
                    "step_order": 4,
                    "mode": "SEQUENTIAL",
                    "approver_kind": "ORG_NODE",
                    "approver_org_node_id": third_branch_node.id,
                    "label": f"دائرة: {third_branch_node.name_ar}",
                },
                {
                    "step_order": 5,
                    "mode": "SEQUENTIAL",
                    "approver_kind": "ORG_NODE",
                    "approver_org_node_id": self.department_a2.id,
                    "label": f"دائرة: {self.department_a2.name_ar}",
                    "reason": DYNAMIC_RETURN_REASON,
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
            [2, 3, 4],
        )

        with self.assertRaisesRegex(ValueError, "اختر دائرة واحدة على الأقل"):
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
            selected_dynamic_branch_step_orders=[2, 4],
        )
        db.session.flush()

        first_selected_branch = WorkflowInstanceStep.query.filter_by(
            instance_id=instance.id,
            step_order=2,
        ).one()
        skipped_branch = WorkflowInstanceStep.query.filter_by(
            instance_id=instance.id,
            step_order=3,
        ).one()
        second_selected_branch = WorkflowInstanceStep.query.filter_by(
            instance_id=instance.id,
            step_order=4,
        ).one()
        skipped_return_branch = WorkflowInstanceStep.query.filter_by(
            instance_id=instance.id,
            step_order=5,
        ).one()
        self.assertEqual(first_selected_branch.status, "PENDING")
        self.assertEqual(skipped_branch.status, "SKIPPED")
        self.assertEqual(second_selected_branch.status, "PENDING")
        self.assertEqual(skipped_return_branch.status, "SKIPPED")
        self.assertEqual(instance.current_step_order, 2)
        self.assertIsNotNone(AuditLog.query.filter_by(
            request_id=request.id,
            action="DYNAMIC_BRANCH_SELECTED",
        ).first())

        decide_step(
            request.id,
            2,
            self.source_manager.id,
            "APPROVED",
            auto_commit=False,
        )
        db.session.flush()
        self.assertEqual(instance.current_step_order, 4)

        decide_step(
            request.id,
            4,
            self.cross_target.id,
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
        template = WorkflowTemplate(
            name="مسار هيكلي محفوظ",
            created_by_id=self.requester.id,
            sla_days_default=6,
        )
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
        self.assertEqual(step.sla_days, 6)
        self.assertIsNotNone(step.due_at)
        self.assertIsNotNone(Notification.query.filter_by(user_id=self.target_manager.id).first())

    def test_dynamic_path_applies_selected_sla_to_every_runtime_step(self):
        result = build_dynamic_target_path(
            self.requester,
            [f"USER:{self.same_target.id}"],
            sla_days=8,
        )

        self.assertEqual(result["errors"], [])
        self.assertTrue(result["steps"])
        self.assertEqual(
            {step.get("sla_days") for step in result["steps"]},
            {8},
        )

    def test_dynamic_sla_starts_only_when_each_step_becomes_active(self):
        request_row = WorkflowRequest(
            requester_id=self.requester.id,
            title="طلب SLA ديناميكي",
            status="DRAFT",
            confidentiality="NORMAL",
        )
        db.session.add(request_row)
        db.session.flush()

        start_workflow_for_request(
            request_row,
            None,
            created_by_user_id=self.requester.id,
            runtime_steps=[
                {
                    "step_order": 1,
                    "approver_kind": "USER",
                    "approver_user_id": self.same_target.id,
                    "sla_days": 4,
                },
                {
                    "step_order": 2,
                    "approver_kind": "USER",
                    "approver_user_id": self.cross_target.id,
                    "sla_days": 4,
                },
            ],
        )
        db.session.commit()

        instance = WorkflowInstance.query.filter_by(request_id=request_row.id).one()
        first_step, second_step = WorkflowInstanceStep.query.filter_by(
            instance_id=instance.id,
        ).order_by(WorkflowInstanceStep.step_order.asc()).all()
        self.assertEqual(first_step.sla_days, 4)
        self.assertIsNotNone(first_step.due_at)
        self.assertEqual(second_step.sla_days, 4)
        self.assertIsNone(second_step.due_at)

        decide_step(
            request_row.id,
            first_step.step_order,
            self.same_target.id,
            "APPROVED",
            auto_commit=False,
        )
        db.session.flush()

        self.assertEqual(instance.current_step_order, 2)
        self.assertIsNotNone(second_step.due_at)
        remaining_seconds = (second_step.due_at - first_step.decided_at).total_seconds()
        self.assertGreater(remaining_seconds, (4 * 86400) - 2)
        self.assertLessEqual(remaining_seconds, 4 * 86400)

    def test_alert_levels_resolve_runtime_target_and_assistant_secretary(self):
        assistant_type = self._node_type("SEC_GEN_ASSIST", "مساعد الأمين العام", 15)
        assistant_user = self._user("assistant@example.test", "مساعد الأمين العام")
        assistant_node = self._node("مساعد الأمين العام للاختبار", assistant_type, self.root)
        directorate_type = OrgNodeType.query.filter_by(code="DIRECTORATE").one()
        department_type = OrgNodeType.query.filter_by(code="DEPARTMENT").one()
        child_directorate = self._node("إدارة تابعة للمساعد", directorate_type, assistant_node)
        child_department = self._node("دائرة تابعة للمساعد", department_type, child_directorate)
        db.session.add(OrgNodeManager(
            node_id=assistant_node.id,
            manager_user_id=assistant_user.id,
        ))
        db.session.add_all([
            OrgNodeManager(
                node_id=child_directorate.id,
                manager_user_id=self.target_manager.id,
            ),
            OrgNodeManager(
                node_id=child_department.id,
                manager_user_id=self.cross_target.id,
            ),
        ])
        self.cross_target.org_node_id = child_department.id

        request_row = WorkflowRequest(
            requester_id=self.requester.id,
            title="طلب تنبيه ثانٍ",
            status="IN_PROGRESS",
            confidentiality="NORMAL",
        )
        db.session.add(request_row)
        db.session.flush()

        instance = WorkflowInstance(
            request_id=request_row.id,
            template_id=None,
            current_step_order=1,
        )
        db.session.add(instance)
        db.session.flush()
        dynamic_step = WorkflowInstanceStep(
            instance_id=instance.id,
            step_order=1,
            approver_kind="ORG_NODE",
            approver_org_node_id=child_department.id,
            status="PENDING",
        )
        user_step = WorkflowInstanceStep(
            instance_id=1,
            step_order=1,
            approver_kind="USER",
            approver_user_id=self.cross_target.id,
            status="PENDING",
        )
        db.session.add(dynamic_step)
        db.session.commit()

        self.assertEqual(
            _hierarchy_manager_user_ids(request_row, dynamic_step, {"SEC_GEN_ASSIST"}),
            [assistant_user.id],
        )
        self.assertEqual(
            _hierarchy_manager_user_ids(request_row, user_step, {"SEC_GEN_ASSIST"}),
            [assistant_user.id],
        )

        for level in (1, 2):
            with self.app.test_request_context(
                method="POST",
                data={
                    "alert_level": str(level),
                    "category": "SLA_RISK",
                    "description": f"اختبار التنبيه {level}",
                },
            ):
                with patch("workflow.routes.current_user", self.requester), patch(
                    "workflow.routes._user_can_view_request", return_value=True
                ), patch(
                    "workflow.routes.get_effective_user", return_value=self.requester
                ), patch(
                    "workflow.routes.url_for",
                    return_value=f"/workflow/request/{request_row.id}",
                ), patch("workflow.routes.redirect", return_value="ok"):
                    self.assertEqual(escalate_request.__wrapped__(request_row.id), "ok")

        alerts = RequestEscalation.query.filter_by(request_id=request_row.id).order_by(
            RequestEscalation.alert_level.asc(),
        ).all()
        self.assertEqual([alert.alert_level for alert in alerts], [1, 2])
        self.assertEqual(
            {int(user_id) for user_id in alerts[0].targets.split(",")},
            {self.cross_target.id, self.target_manager.id},
        )
        self.assertEqual(alerts[1].targets, str(assistant_user.id))

    def test_legacy_directorate_alert_matches_approved_hierarchy_by_name(self):
        assistant_type = self._node_type("SEC_GEN_ASSIST", "مساعد الأمين العام", 15)
        assistant_user = self._user("kholoud@example.test", "خلود")
        assistant_node = self._node(
            "مساعد الأمين العام للمنظمات والبرامج والمشاريع والإدارات التخصصية",
            assistant_type,
            self.root,
        )
        approved_programs_node = self._node(
            "الإدارة العامة للبرامج والمشاريع",
            self.directorate_a.type,
            assistant_node,
        )
        db.session.add(OrgNodeManager(
            node_id=assistant_node.id,
            manager_user_id=assistant_user.id,
        ))

        legacy_organization = Organization(name_ar="الأمانة العامة", is_active=True)
        db.session.add(legacy_organization)
        db.session.flush()
        legacy_directorate = Directorate(
            organization_id=legacy_organization.id,
            name_ar=approved_programs_node.name_ar,
            is_active=True,
        )
        db.session.add(legacy_directorate)
        db.session.flush()

        request_row = WorkflowRequest(
            requester_id=self.requester.id,
            title="طلب قديم على إدارة البرامج والمشاريع",
            status="IN_PROGRESS",
            confidentiality="NORMAL",
        )
        legacy_step = WorkflowInstanceStep(
            instance_id=1,
            step_order=1,
            approver_kind="DIRECTORATE",
            approver_directorate_id=legacy_directorate.id,
            approver_role="directorate_head",
            status="PENDING",
        )
        db.session.add(request_row)
        db.session.commit()

        self.assertEqual(
            _hierarchy_manager_user_ids(request_row, legacy_step, {"SEC_GEN_ASSIST"}),
            [assistant_user.id],
        )

    def test_structural_route_traverses_common_parent_once(self):
        route = structural_route_nodes(self.department_a1.id, self.department_b.id)
        route_ids = [node.id for node in route]
        self.assertEqual(route_ids.count(self.root.id), 1)
        self.assertEqual(route_ids[0], self.department_a1.id)
        self.assertEqual(route_ids[-1], self.department_b.id)


if __name__ == "__main__":
    unittest.main()
