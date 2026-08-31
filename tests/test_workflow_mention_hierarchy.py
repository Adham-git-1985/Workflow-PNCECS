import unittest
from unittest.mock import patch

from flask import Flask

from extensions import db
from models import (
    AuditLog,
    OrgNode,
    OrgNodeAssignment,
    OrgNodeManager,
    OrgNodeType,
    User,
    WorkflowInstance,
    WorkflowInstanceStep,
    WorkflowRequest,
    WorkflowStepTask,
)
from workflow import workflow_bp
from workflow.routes import (
    MENTION_ACCESS_ACTION,
    MENTION_ACCESS_REVOKED_ACTION,
    MENTION_HIERARCHY_DENIED,
    _can_mention_user_by_hierarchy,
    _filter_mention_users_by_hierarchy,
    _grant_mention_access,
    _mentioned_user_ids_for_request,
    _user_can_view_request,
    mention_search,
    remove_request_mention,
)


def _unwrapped(function):
    while hasattr(function, "__wrapped__"):
        function = function.__wrapped__
    return function


class WorkflowMentionHierarchyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY="workflow-mention-hierarchy-test",
        )
        cls.app.register_blueprint(workflow_bp, url_prefix="/workflow")
        db.init_app(cls.app)
        cls.context = cls.app.app_context()
        cls.context.push()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.context.pop()

    def setUp(self):
        db.session.remove()
        db.drop_all()
        db.create_all()

        node_type = OrgNodeType(
            code="TEST_NODE",
            name_ar="مستوى اختباري",
            is_active=True,
            show_in_chart=True,
            show_in_routes=True,
            allow_in_approvals=True,
        )
        db.session.add(node_type)
        db.session.flush()

        self.root = OrgNode(type_id=node_type.id, name_ar="الجذر", is_active=True)
        db.session.add(self.root)
        db.session.flush()
        self.directorate_a = OrgNode(
            type_id=node_type.id,
            parent_id=self.root.id,
            name_ar="إدارة أ",
            is_active=True,
        )
        self.directorate_b = OrgNode(
            type_id=node_type.id,
            parent_id=self.root.id,
            name_ar="إدارة ب",
            is_active=True,
        )
        db.session.add_all((self.directorate_a, self.directorate_b))
        db.session.flush()
        self.department_a = OrgNode(
            type_id=node_type.id,
            parent_id=self.directorate_a.id,
            name_ar="دائرة أ",
            is_active=True,
        )
        self.department_b = OrgNode(
            type_id=node_type.id,
            parent_id=self.directorate_b.id,
            name_ar="دائرة ب",
            is_active=True,
        )
        db.session.add_all((self.department_a, self.department_b))
        db.session.flush()
        self.section_a = OrgNode(
            type_id=node_type.id,
            parent_id=self.department_a.id,
            name_ar="قسم أ",
            is_active=True,
        )
        db.session.add(self.section_a)

        self.department_user = self._user("department@example.test", "مستخدم الدائرة")
        self.peer_user = self._user("peer@example.test", "مستخدم موازٍ")
        self.lower_user = self._user("lower@example.test", "مستخدم أدنى")
        self.higher_user = self._user("higher@example.test", "مستخدم أعلى")
        self.unassigned_user = self._user("unassigned@example.test", "دون تعيين")
        self.manager_user = self._user("manager@example.test", "مدير الإدارة")
        db.session.flush()
        db.session.add_all((
            OrgNodeAssignment(
                user_id=self.department_user.id,
                node_id=self.department_a.id,
                is_primary=True,
            ),
            OrgNodeAssignment(
                user_id=self.peer_user.id,
                node_id=self.department_b.id,
                is_primary=True,
            ),
            OrgNodeAssignment(
                user_id=self.lower_user.id,
                node_id=self.section_a.id,
                is_primary=True,
            ),
            OrgNodeAssignment(
                user_id=self.higher_user.id,
                node_id=self.directorate_a.id,
                is_primary=True,
            ),
            OrgNodeAssignment(
                user_id=self.manager_user.id,
                node_id=self.section_a.id,
                is_primary=True,
            ),
            OrgNodeManager(
                node_id=self.directorate_a.id,
                manager_user_id=self.manager_user.id,
            ),
        ))
        db.session.commit()

    @staticmethod
    def _user(email, name):
        user = User(
            email=email,
            name=name,
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        db.session.add(user)
        return user

    def test_same_and_lower_levels_are_allowed_but_higher_is_blocked(self):
        allowed, blocked = _filter_mention_users_by_hierarchy(
            self.department_user,
            [self.peer_user, self.lower_user, self.higher_user],
        )

        self.assertEqual({user.id for user in allowed}, {
            self.peer_user.id,
            self.lower_user.id,
        })
        self.assertEqual({user.id for user in blocked}, {self.higher_user.id})

    def test_manager_position_uses_its_higher_node(self):
        self.assertTrue(
            _can_mention_user_by_hierarchy(self.manager_user, self.department_user)
        )
        self.assertFalse(
            _can_mention_user_by_hierarchy(self.department_user, self.manager_user)
        )

    def test_user_without_org_assignment_cannot_be_a_cross_hierarchy_target(self):
        self.assertFalse(
            _can_mention_user_by_hierarchy(self.department_user, self.unassigned_user)
        )

    def test_manual_mention_cannot_grant_access_to_a_higher_level(self):
        request_row = WorkflowRequest(
            requester_id=self.department_user.id,
            title="طلب اختبار المنشن",
            status="IN_PROGRESS",
        )
        db.session.add(request_row)
        db.session.commit()

        note = f"@{self.higher_user.email} @{self.lower_user.email}"
        with self.app.test_request_context("/workflow/request/1/note"), patch(
            "workflow.routes.current_user", self.department_user
        ), patch("workflow.routes._send_mention_internal_message"), patch(
            "workflow.routes.emit_event"
        ):
            added, unresolved = _grant_mention_access(
                request_row,
                None,
                note,
                step_order=None,
            )

        self.assertEqual([user.id for user in added], [self.lower_user.id])
        self.assertTrue(any(MENTION_HIERARCHY_DENIED in item for item in unresolved))
        granted_user_ids = {
            user_id
            for (user_id,) in db.session.query(AuditLog.target_id)
            .filter(
                AuditLog.request_id == request_row.id,
                AuditLog.action == MENTION_ACCESS_ACTION,
            )
            .all()
        }
        self.assertEqual(granted_user_ids, {self.lower_user.id})

    def test_mention_search_excludes_users_at_higher_levels(self):
        search = _unwrapped(mention_search)

        with self.app.test_request_context(
            f"/workflow/mentions/search?q={self.higher_user.email}"
        ), patch("workflow.routes.current_user", self.department_user):
            blocked_results = search().get_json()["results"]

        with self.app.test_request_context(
            f"/workflow/mentions/search?q={self.lower_user.email}"
        ), patch("workflow.routes.current_user", self.department_user):
            allowed_results = search().get_json()["results"]

        self.assertEqual(blocked_results, [])
        self.assertEqual([item["label"] for item in allowed_results], [self.lower_user.name])

    def test_removed_mention_can_be_added_again_as_pending_task(self):
        request_row = WorkflowRequest(
            requester_id=self.department_user.id,
            title="طلب اختبار حذف المنشن",
            status="IN_PROGRESS",
        )
        db.session.add(request_row)
        db.session.flush()
        instance = WorkflowInstance(request_id=request_row.id, current_step_order=1, is_completed=False)
        db.session.add(instance)
        db.session.flush()
        db.session.add(WorkflowInstanceStep(
            instance_id=instance.id,
            step_order=1,
            mode="SEQUENTIAL",
            approver_kind="USER",
            approver_user_id=self.department_user.id,
            status="PENDING",
        ))

        with self.app.test_request_context(f"/workflow/request/{request_row.id}/note"), patch(
            "workflow.routes.current_user", self.department_user
        ), patch("workflow.routes._send_mention_internal_message"), patch(
            "workflow.routes.emit_event"
        ):
            added, unresolved = _grant_mention_access(
                request_row,
                instance,
                f"@{self.lower_user.email}",
                step_order=1,
            )

        self.assertEqual([user.id for user in added], [self.lower_user.id])
        self.assertEqual(unresolved, [])
        db.session.commit()

        remove = _unwrapped(remove_request_mention)
        with self.app.test_request_context(
            f"/workflow/request/{request_row.id}/mention/{self.lower_user.id}/remove",
            method="POST",
        ), patch("workflow.routes.current_user", self.department_user):
            response = remove(request_row.id, self.lower_user.id)

        self.assertEqual(response.status_code, 302)
        self.assertNotIn(self.lower_user.id, _mentioned_user_ids_for_request(request_row.id))
        self.assertFalse(_user_can_view_request(self.lower_user, request_row))
        task = WorkflowStepTask.query.filter_by(
            instance_id=instance.id,
            step_order=1,
            assignee_user_id=self.lower_user.id,
        ).one()
        self.assertEqual(task.status, "BYPASSED")
        self.assertEqual(
            AuditLog.query.filter_by(
                request_id=request_row.id,
                action=MENTION_ACCESS_REVOKED_ACTION,
                target_id=self.lower_user.id,
            ).count(),
            1,
        )

        with self.app.test_request_context(f"/workflow/request/{request_row.id}/note"), patch(
            "workflow.routes.current_user", self.department_user
        ), patch("workflow.routes._send_mention_internal_message"), patch(
            "workflow.routes.emit_event"
        ):
            added, unresolved = _grant_mention_access(
                request_row,
                instance,
                f"@{self.lower_user.email}",
                step_order=1,
            )

        self.assertEqual([user.id for user in added], [self.lower_user.id])
        self.assertEqual(unresolved, [])
        db.session.flush()
        self.assertIn(self.lower_user.id, _mentioned_user_ids_for_request(request_row.id))
        self.assertTrue(_user_can_view_request(self.lower_user, request_row))
        self.assertEqual(task.status, "PENDING")
        self.assertEqual(task.response, "NONE")


if __name__ == "__main__":
    unittest.main()
