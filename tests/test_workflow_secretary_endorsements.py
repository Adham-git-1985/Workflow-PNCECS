import unittest
from unittest.mock import patch

from flask import Flask

from extensions import db
from models import (
    AuditLog,
    User,
    UserPermission,
    WorkflowInstance,
    WorkflowInstanceStep,
    WorkflowQuickEndorsement,
    WorkflowRequest,
)
from portal.perm_defs import ALL_KEYS as PORTAL_ALL_KEYS, PERMS as PORTAL_PERMS
from workflow import workflow_bp
from workflow.routes import (
    SECRETARY_ENDORSEMENTS_PERMISSION,
    _get_secretary_endorsements,
    _secretary_endorsement_note,
    add_request_note,
    manage_secretary_endorsements,
)


def _unwrapped(function):
    while hasattr(function, "__wrapped__"):
        function = function.__wrapped__
    return function


class WorkflowSecretaryEndorsementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY="workflow-secretary-endorsements-test",
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

        self.requester = self._user("requester@example.test", "مقدم الطلب")
        self.secretary = self._user("secretary@example.test", "الأمين العام")
        db.session.flush()
        db.session.add(UserPermission(
            user_id=self.secretary.id,
            key=SECRETARY_ENDORSEMENTS_PERMISSION,
            is_allowed=True,
        ))
        self.request_row = WorkflowRequest(
            requester_id=self.requester.id,
            title="معاملة اختبار التأشيرة",
            status="IN_PROGRESS",
        )
        db.session.add(self.request_row)
        db.session.flush()
        self.instance = WorkflowInstance(
            request_id=self.request_row.id,
            current_step_order=1,
            is_completed=False,
        )
        db.session.add(self.instance)
        db.session.flush()
        db.session.add(WorkflowInstanceStep(
            instance_id=self.instance.id,
            step_order=1,
            mode="SEQUENTIAL",
            approver_kind="USER",
            approver_user_id=self.secretary.id,
            status="PENDING",
        ))
        db.session.commit()

    @staticmethod
    def _user(email, name):
        user = User(email=email, name=name, password_hash="not-used-in-test", role="EMPLOYEE")
        db.session.add(user)
        return user

    def test_take_action_endorsement_uses_the_requested_wording(self):
        endorsement = _get_secretary_endorsements()[0]
        self.assertEqual(endorsement.text, "لاتخاذ اللازم")
        self.assertEqual(_secretary_endorsement_note(endorsement.id), "لاتخاذ اللازم")

    def test_endorsement_permission_is_available_in_the_permission_editor(self):
        self.assertIn(SECRETARY_ENDORSEMENTS_PERMISSION, PORTAL_ALL_KEYS)
        definitions = [perm for group in PORTAL_PERMS.values() for perm in group]
        definition = next(perm for perm in definitions if perm.key == SECRETARY_ENDORSEMENTS_PERMISSION)
        self.assertEqual(definition.label, "تأشيرات الأمين العام السريعة")

    def test_authorized_user_can_add_a_quick_endorsement_without_changing_the_route(self):
        endorsement = _get_secretary_endorsements()[0]
        add_note = _unwrapped(add_request_note)
        with self.app.test_request_context(
            f"/workflow/request/{self.request_row.id}/note",
            method="POST",
            data={"endorsement_id": str(endorsement.id)},
        ), patch("workflow.routes.current_user", self.secretary), patch(
            "workflow.routes.emit_event"
        ):
            response = add_note(self.request_row.id)

        saved_note = AuditLog.query.filter_by(
            request_id=self.request_row.id,
            action="WORKFLOW_COMMENT",
            user_id=self.secretary.id,
        ).one()
        step = WorkflowInstanceStep.query.filter_by(instance_id=self.instance.id, step_order=1).one()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(saved_note.note, "لاتخاذ اللازم")
        self.assertEqual(self.request_row.status, "IN_PROGRESS")
        self.assertEqual(self.instance.current_step_order, 1)
        self.assertEqual(step.status, "PENDING")

    def test_authorized_user_can_add_and_remove_a_shared_endorsement(self):
        manage = _unwrapped(manage_secretary_endorsements)
        with self.app.test_request_context(
            "/workflow/endorsements/manage",
            method="POST",
            data={
                "action": "ADD",
                "endorsement_text": "للتحويل إلى الجهة المختصة",
                "request_id": str(self.request_row.id),
            },
        ), patch("workflow.routes.current_user", self.secretary):
            response = manage()

        added = WorkflowQuickEndorsement.query.filter_by(text="للتحويل إلى الجهة المختصة").one()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(added.is_active)

        with self.app.test_request_context(
            "/workflow/endorsements/manage",
            method="POST",
            data={
                "action": "REMOVE",
                "endorsement_id": str(added.id),
                "request_id": str(self.request_row.id),
            },
        ), patch("workflow.routes.current_user", self.secretary):
            response = manage()

        self.assertEqual(response.status_code, 302)
        self.assertFalse(WorkflowQuickEndorsement.query.get(added.id).is_active)


if __name__ == "__main__":
    unittest.main()
