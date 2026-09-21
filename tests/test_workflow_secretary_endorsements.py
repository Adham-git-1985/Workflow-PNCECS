import unittest
from unittest.mock import patch

from flask import Flask
from werkzeug.exceptions import BadRequest, Forbidden

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
    EMPLOYEE_ENDORSEMENTS_PERMISSION,
    ENDORSEMENT_AUDIENCE_EMPLOYEE,
    ENDORSEMENT_AUDIENCE_SECRETARY,
    SECRETARY_ENDORSEMENTS_PERMISSION,
    _can_manage_quick_endorsements,
    _can_use_employee_endorsements,
    _employee_endorsement_notes,
    _get_employee_endorsements,
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
        self.employee = self._user("employee@example.test", "موظف مخوّل")
        self.admin = User(
            email="admin@example.test",
            name="مدير النظام",
            password_hash="not-used-in-test",
            role="ADMIN",
        )
        self.super_admin = User(
            email="super-admin@example.test",
            name="مدير النظام الأعلى",
            password_hash="not-used-in-test",
            role="SUPER_ADMIN",
        )
        db.session.add_all((self.admin, self.super_admin))
        db.session.flush()
        db.session.add(UserPermission(
            user_id=self.employee.id,
            key=EMPLOYEE_ENDORSEMENTS_PERMISSION,
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
            approver_user_id=self.employee.id,
            status="PENDING",
        ))
        db.session.commit()
        self.secretary_endorsements = _get_secretary_endorsements(
            seed_defaults=True,
            seeded_by=self.admin,
        )
        self.employee_endorsement = WorkflowQuickEndorsement(
            audience=ENDORSEMENT_AUDIENCE_EMPLOYEE,
            text="للمتابعة من الموظف",
            sort_order=1,
            created_by_id=self.admin.id,
        )
        db.session.add(self.employee_endorsement)
        db.session.commit()

    @staticmethod
    def _user(email, name):
        user = User(email=email, name=name, password_hash="not-used-in-test", role="EMPLOYEE")
        db.session.add(user)
        return user

    def test_take_action_endorsement_uses_the_requested_wording(self):
        endorsement = self.secretary_endorsements[0]
        self.assertEqual(endorsement.text, "لاتخاذ اللازم")
        self.assertEqual(endorsement.audience, ENDORSEMENT_AUDIENCE_SECRETARY)
        self.assertEqual(_secretary_endorsement_note(endorsement.id), "لاتخاذ اللازم")

    def test_only_an_admin_can_seed_default_endorsements(self):
        WorkflowQuickEndorsement.query.delete()
        db.session.commit()

        self.assertEqual(
            _get_secretary_endorsements(seed_defaults=True, seeded_by=self.employee),
            [],
        )
        seeded = _get_secretary_endorsements(seed_defaults=True, seeded_by=self.admin)
        self.assertEqual(len(seeded), 4)
        self.assertTrue(all(row.created_by_id == self.admin.id for row in seeded))
        self.assertTrue(all(row.audience == ENDORSEMENT_AUDIENCE_SECRETARY for row in seeded))

    def test_endorsement_permission_is_available_in_the_permission_editor(self):
        self.assertIn(SECRETARY_ENDORSEMENTS_PERMISSION, PORTAL_ALL_KEYS)
        self.assertIn(EMPLOYEE_ENDORSEMENTS_PERMISSION, PORTAL_ALL_KEYS)
        definitions = [perm for group in PORTAL_PERMS.values() for perm in group]
        secretary_definition = next(perm for perm in definitions if perm.key == SECRETARY_ENDORSEMENTS_PERMISSION)
        self.assertEqual(secretary_definition.label, "تأشيرات الأمين العام السريعة")
        employee_definition = next(perm for perm in definitions if perm.key == EMPLOYEE_ENDORSEMENTS_PERMISSION)
        self.assertEqual(employee_definition.label, "تأشيرات الموظفين")
        self.assertTrue(employee_definition.user_only)

    def test_employee_endorsements_are_independent_from_secretary_endorsements(self):
        self.assertEqual(
            [row.text for row in _get_employee_endorsements()],
            ["للمتابعة من الموظف"],
        )
        self.assertEqual(
            _employee_endorsement_notes([self.secretary_endorsements[0].id]),
            None,
        )
        self.assertEqual(
            _secretary_endorsement_note(self.employee_endorsement.id),
            None,
        )

    def test_authorized_employee_can_add_an_employee_endorsement_as_a_comment(self):
        endorsement = self.employee_endorsement
        add_note = _unwrapped(add_request_note)
        with self.app.test_request_context(
            f"/workflow/request/{self.request_row.id}/note",
            method="POST",
            data={
                "endorsement_id": str(endorsement.id),
                "endorsement_audience": ENDORSEMENT_AUDIENCE_EMPLOYEE,
            },
        ), patch("workflow.routes.current_user", self.employee), patch(
            "workflow.routes.emit_event"
        ):
            response = add_note(self.request_row.id)

        saved_note = AuditLog.query.filter_by(
            request_id=self.request_row.id,
            action="WORKFLOW_COMMENT",
            user_id=self.employee.id,
        ).one()
        step = WorkflowInstanceStep.query.filter_by(instance_id=self.instance.id, step_order=1).one()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(saved_note.note, endorsement.text)
        self.assertEqual(self.request_row.status, "IN_PROGRESS")
        self.assertEqual(self.instance.current_step_order, 1)
        self.assertEqual(step.status, "PENDING")

    def test_authorized_user_can_add_multiple_quick_endorsements_in_one_multiline_comment(self):
        second_endorsement = WorkflowQuickEndorsement(
            audience=ENDORSEMENT_AUDIENCE_EMPLOYEE,
            text="إبداء الرأي من الموظف",
            sort_order=2,
            created_by_id=self.admin.id,
        )
        db.session.add(second_endorsement)
        db.session.commit()
        first_endorsement = self.employee_endorsement
        add_note = _unwrapped(add_request_note)
        with self.app.test_request_context(
            f"/workflow/request/{self.request_row.id}/note",
            method="POST",
            data={
                "endorsement_ids": [str(first_endorsement.id), str(second_endorsement.id)],
                "endorsement_audience": ENDORSEMENT_AUDIENCE_EMPLOYEE,
            },
        ), patch("workflow.routes.current_user", self.employee), patch(
            "workflow.routes.emit_event"
        ):
            response = add_note(self.request_row.id)

        saved_note = AuditLog.query.filter_by(
            request_id=self.request_row.id,
            action="WORKFLOW_COMMENT",
            user_id=self.employee.id,
        ).one()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            saved_note.note,
            f"{first_endorsement.text}\n{second_endorsement.text}",
        )

    def test_employee_cannot_submit_a_secretary_endorsement(self):
        add_note = _unwrapped(add_request_note)
        with self.app.test_request_context(
            f"/workflow/request/{self.request_row.id}/note",
            method="POST",
            data={
                "endorsement_id": str(self.secretary_endorsements[0].id),
                "endorsement_audience": ENDORSEMENT_AUDIENCE_EMPLOYEE,
            },
        ), patch("workflow.routes.current_user", self.employee):
            with self.assertRaises(BadRequest):
                add_note(self.request_row.id)

    def test_only_admin_and_super_admin_can_manage_shared_endorsements(self):
        manage = _unwrapped(manage_secretary_endorsements)
        self.assertTrue(_can_use_employee_endorsements(self.employee))
        self.assertFalse(_can_use_employee_endorsements(self.admin))
        self.assertFalse(_can_use_employee_endorsements(self.super_admin))
        self.assertFalse(_can_manage_quick_endorsements(self.employee))
        self.assertTrue(_can_manage_quick_endorsements(self.admin))
        self.assertTrue(_can_manage_quick_endorsements(self.super_admin))

        with self.app.test_request_context(
            "/workflow/endorsements/manage",
            method="POST",
            data={
                "action": "ADD",
                "endorsement_text": "تأشيرة لا يحق للموظف إنشاؤها",
                "endorsement_audience": ENDORSEMENT_AUDIENCE_EMPLOYEE,
                "request_id": str(self.request_row.id),
            },
        ), patch("workflow.routes.current_user", self.employee):
            with self.assertRaises(Forbidden):
                manage()

        with self.app.test_request_context(
            "/workflow/endorsements/manage",
            method="POST",
            data={
                "action": "ADD",
                "endorsement_text": "للتحويل إلى الجهة المختصة",
                "endorsement_audience": ENDORSEMENT_AUDIENCE_EMPLOYEE,
                "request_id": str(self.request_row.id),
            },
        ), patch("workflow.routes.current_user", self.admin):
            response = manage()

        added = WorkflowQuickEndorsement.query.filter_by(
            audience=ENDORSEMENT_AUDIENCE_EMPLOYEE,
            text="للتحويل إلى الجهة المختصة",
        ).one()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(added.is_active)
        self.assertEqual(added.created_by_id, self.admin.id)

        # The administrator may manage the same wording in the independent
        # Secretary-General list without affecting the employee list.
        with self.app.test_request_context(
            "/workflow/endorsements/manage",
            method="POST",
            data={
                "action": "ADD",
                "endorsement_text": "للتحويل إلى الجهة المختصة",
                "endorsement_audience": ENDORSEMENT_AUDIENCE_SECRETARY,
                "request_id": str(self.request_row.id),
            },
        ), patch("workflow.routes.current_user", self.admin):
            response = manage()

        secretary_copy = WorkflowQuickEndorsement.query.filter_by(
            audience=ENDORSEMENT_AUDIENCE_SECRETARY,
            text="للتحويل إلى الجهة المختصة",
        ).one()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(secretary_copy.is_active)

        with self.app.test_request_context(
            "/workflow/endorsements/manage",
            method="POST",
            data={
                "action": "REMOVE",
                "endorsement_id": str(added.id),
                "endorsement_audience": ENDORSEMENT_AUDIENCE_EMPLOYEE,
                "request_id": str(self.request_row.id),
            },
        ), patch("workflow.routes.current_user", self.super_admin):
            response = manage()

        self.assertEqual(response.status_code, 302)
        self.assertFalse(WorkflowQuickEndorsement.query.get(added.id).is_active)
        self.assertTrue(WorkflowQuickEndorsement.query.get(secretary_copy.id).is_active)

    def test_same_wording_can_be_managed_independently_for_each_audience(self):
        wording = "للدراسة"
        secretary = WorkflowQuickEndorsement(
            audience=ENDORSEMENT_AUDIENCE_SECRETARY,
            text=wording,
            sort_order=99,
            created_by_id=self.admin.id,
        )
        employee = WorkflowQuickEndorsement(
            audience=ENDORSEMENT_AUDIENCE_EMPLOYEE,
            text=wording,
            sort_order=99,
            created_by_id=self.admin.id,
        )
        db.session.add_all((secretary, employee))
        db.session.commit()

        self.assertEqual(_secretary_endorsement_note(secretary.id), wording)
        self.assertEqual(_employee_endorsement_notes([employee.id]), [wording])


if __name__ == "__main__":
    unittest.main()
