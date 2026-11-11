import unittest
from datetime import datetime, timedelta

from flask import Flask

from extensions import db
from models import RolePermission, User, UserPermission, WorkflowRequest, WorkflowTemplate
from portal.perm_defs import PERMS
from admin.masterdata import PERM_EXTRA_KEYS
from workflow.temporary_delete import (
    PERMANENT_DELETE_PERMISSION,
    TEMPORARY_DELETE_PERMISSION,
    can_permanently_delete_workflow,
    can_delete_workflow_request,
    can_delete_workflow_template,
)
from permissions import _user_is_super_admin


class WorkflowTemporaryDeleteTests(unittest.TestCase):
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
        # User id 1 is treated as the bootstrap super-admin by legacy rules.
        self.super_admin = self._user("bootstrap@example.test", role="SUPER_ADMIN")
        self.admin = self._user("admin@example.test", role="ADMIN")
        self.owner = self._user("owner@example.test")
        self.other_user = self._user("other@example.test")
        self.permanent_user = self._user("permanent@example.test")
        db.session.add(UserPermission(
            user_id=self.owner.id,
            key=TEMPORARY_DELETE_PERMISSION,
            is_allowed=True,
        ))
        db.session.add(UserPermission(
            user_id=self.other_user.id,
            key=TEMPORARY_DELETE_PERMISSION,
            is_allowed=True,
        ))
        db.session.add(UserPermission(
            user_id=self.permanent_user.id,
            key=PERMANENT_DELETE_PERMISSION,
            is_allowed=True,
        ))
        db.session.commit()

    @staticmethod
    def _user(email, role="EMPLOYEE"):
        user = User(email=email, password_hash="not-used-in-test", role=role)
        db.session.add(user)
        db.session.flush()
        return user

    def test_owner_can_revoke_request_during_first_hour(self):
        now = datetime.utcnow()
        request_row = WorkflowRequest(
            requester_id=self.owner.id,
            title="طلب قابل للإلغاء",
            status="DRAFT",
            created_at=now - timedelta(minutes=59),
        )

        self.assertTrue(can_delete_workflow_request(self.owner, request_row, now=now))

    def test_temporary_request_revoke_expires_after_one_hour(self):
        now = datetime.utcnow()
        request_row = WorkflowRequest(
            requester_id=self.owner.id,
            title="طلب منتهي المهلة",
            status="DRAFT",
            created_at=now - timedelta(hours=1),
        )

        self.assertFalse(can_delete_workflow_request(self.owner, request_row, now=now))

    def test_temporary_revoke_cannot_delete_another_users_request(self):
        now = datetime.utcnow()
        request_row = WorkflowRequest(
            requester_id=self.owner.id,
            title="طلب المالك",
            status="DRAFT",
            created_at=now - timedelta(minutes=10),
        )

        self.assertFalse(can_delete_workflow_request(self.other_user, request_row, now=now))

    def test_super_admin_keeps_request_deletion_access_after_window(self):
        now = datetime.utcnow()
        request_row = WorkflowRequest(
            requester_id=self.owner.id,
            title="طلب قديم",
            status="DRAFT",
            created_at=now - timedelta(days=2),
        )

        self.assertTrue(can_delete_workflow_request(self.super_admin, request_row, now=now))

    def test_permanent_permission_deletes_an_old_request_for_a_selected_user(self):
        now = datetime.utcnow()
        request_row = WorkflowRequest(
            requester_id=self.owner.id,
            title="طلب قديم قابل للحذف النهائي",
            status="DRAFT",
            created_at=now - timedelta(days=2),
        )

        self.assertTrue(can_permanently_delete_workflow(self.permanent_user))
        self.assertTrue(can_delete_workflow_request(self.permanent_user, request_row, now=now))

    def test_permanent_permission_deletes_a_template_for_a_selected_user(self):
        now = datetime.utcnow()
        template = WorkflowTemplate(
            name="مسار قديم قابل للحذف النهائي",
            created_by_id=self.owner.id,
            created_at=now - timedelta(days=2),
        )

        self.assertTrue(can_delete_workflow_template(self.permanent_user, template, now=now))

    def test_admin_and_super_admin_have_permanent_delete_access(self):
        self.assertTrue(can_permanently_delete_workflow(self.admin))
        self.assertTrue(can_permanently_delete_workflow(self.super_admin))

    def test_unselected_user_does_not_have_permanent_delete_access(self):
        self.assertFalse(can_permanently_delete_workflow(self.other_user))

    def test_permanent_delete_permission_is_not_inherited_from_a_role(self):
        db.session.add(RolePermission(
            role=self.other_user.role,
            permission=PERMANENT_DELETE_PERMISSION,
        ))
        db.session.commit()

        self.assertFalse(can_permanently_delete_workflow(self.other_user))

    def test_explicit_normal_role_overrides_legacy_id_one_fallback(self):
        user = type(
            "LegacyIdOneUser",
            (),
            {
                "id": 1,
                "role": "directorate_head",
                "has_role": lambda self, role: False,
            },
        )()

        self.assertFalse(_user_is_super_admin(user))

    def test_owner_can_revoke_template_during_first_hour(self):
        now = datetime.utcnow()
        template = WorkflowTemplate(
            name="مسار قابل للإلغاء",
            created_by_id=self.owner.id,
            created_at=now - timedelta(minutes=10),
        )

        self.assertTrue(can_delete_workflow_template(self.owner, template, now=now))

    def test_temporary_template_revoke_expires_after_one_hour(self):
        now = datetime.utcnow()
        template = WorkflowTemplate(
            name="مسار منتهي المهلة",
            created_by_id=self.owner.id,
            created_at=now - timedelta(hours=1),
        )

        self.assertFalse(can_delete_workflow_template(self.owner, template, now=now))

    def test_permission_is_exposed_as_user_only_in_admin_permissions(self):
        definition = next(
            item
            for group in PERMS.values()
            for item in group
            if item.key == TEMPORARY_DELETE_PERMISSION
        )

        self.assertTrue(definition.user_only)

        self.assertIn(PERMANENT_DELETE_PERMISSION, dict(PERM_EXTRA_KEYS))
        permanent_definition = next(
            item
            for group in PERMS.values()
            for item in group
            if item.key == PERMANENT_DELETE_PERMISSION
        )
        self.assertTrue(permanent_definition.user_only)


if __name__ == "__main__":
    unittest.main()
