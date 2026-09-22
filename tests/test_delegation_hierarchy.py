import unittest
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, g, request
from flask_login import LoginManager, current_user
from jinja2 import ChoiceLoader, DictLoader

from delegation import delegation_bp
from extensions import db
from models import (
    AuditLog,
    Delegation,
    Notification,
    OrgNode,
    OrgNodeAssignment,
    OrgNodeType,
    User,
    UserPermission,
    WorkflowInstance,
    WorkflowInstanceStep,
    WorkflowRequest,
)
from workflow import workflow_bp
from utils.delegation_privacy import audit_display_actor, audit_display_note


class DelegationHierarchyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        project_root = Path(__file__).resolve().parents[1]
        cls.app = Flask(
            __name__,
            template_folder=str(project_root / "templates"),
            static_folder=str(project_root / "static"),
        )
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="delegation-hierarchy-test",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(cls.app)
        login_manager = LoginManager()
        login_manager.init_app(cls.app)

        @login_manager.user_loader
        def load_user(user_id):
            try:
                return db.session.get(User, int(user_id))
            except (TypeError, ValueError):
                return None

        cls.app.register_blueprint(delegation_bp)
        cls.app.register_blueprint(workflow_bp)

        @cls.app.post("/_test/delegation-audit")
        def record_delegated_audit():
            from utils.permissions import get_effective_user

            get_effective_user()
            db.session.add(AuditLog(user_id=current_user.id, action="TEST_DELEGATED_ACTION"))
            db.session.commit()
            return "ok"

        @cls.app.get("/_test/delegation-permission-subject")
        def delegated_permission_subject():
            return "allowed" if current_user.has_perm("PORTAL_READ") else "denied"

        @cls.app.post("/_test/delegation-public-event")
        def delegated_public_event():
            from utils.events import emit_event

            emit_event(
                actor_id=current_user.id,
                action="TEST_DELEGATED_EVENT",
                message=f"Executed by {current_user.full_name}",
                notify_user_id=int(request.form["recipient_id"]),
                auto_commit=False,
            )
            db.session.commit()
            return "ok"

        cls.app.jinja_loader = ChoiceLoader([
            DictLoader({"layout.html": "{% block content %}{% endblock %}"}),
            cls.app.jinja_loader,
        ])
        cls.context = cls.app.app_context()
        cls.context.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.context.pop()

    def setUp(self):
        db.session.remove()
        db.drop_all()
        db.create_all()

        org_type = OrgNodeType(code="ORG", name_ar="المؤسسة")
        admin_type = OrgNodeType(code="ADMIN", name_ar="إداري")
        db.session.add_all((org_type, admin_type))
        db.session.flush()

        root = OrgNode(type_id=org_type.id, name_ar="الجذر")
        actor_node = OrgNode(type_id=admin_type.id, parent=root, name_ar="إدارة الموظف")
        same_level_node = OrgNode(type_id=admin_type.id, parent=root, name_ar="إدارة مماثلة")
        lower_node = OrgNode(type_id=admin_type.id, parent=actor_node, name_ar="قسم أدنى")
        db.session.add_all((root, actor_node, same_level_node, lower_node))

        self.actor = User(
            email="actor@example.test", name="Actor", password_hash="x", role="EMPLOYEE"
        )
        self.higher = User(
            email="higher@example.test", name="Higher", password_hash="x", role="EMPLOYEE"
        )
        self.same = User(
            email="same@example.test", name="Same", password_hash="x", role="EMPLOYEE"
        )
        self.lower = User(
            email="lower@example.test", name="Lower", password_hash="x", role="EMPLOYEE"
        )
        self.unassigned = User(
            email="unassigned@example.test", name="Unassigned", password_hash="x", role="EMPLOYEE"
        )
        db.session.add_all((self.actor, self.higher, self.same, self.lower, self.unassigned))
        db.session.flush()
        db.session.add_all((
            OrgNodeAssignment(user_id=self.actor.id, node_id=actor_node.id, is_primary=True),
            OrgNodeAssignment(user_id=self.higher.id, node_id=root.id, is_primary=True),
            OrgNodeAssignment(user_id=self.same.id, node_id=same_level_node.id, is_primary=True),
            OrgNodeAssignment(user_id=self.lower.id, node_id=lower_node.id, is_primary=True),
        ))
        db.session.commit()

    def _login(self, client):
        for key in ("_login_user", "delegation_checked", "delegations", "delegation", "effective_user"):
            g.pop(key, None)
        with client.session_transaction() as session:
            session.clear()
            session["_user_id"] = str(self.actor.id)
            session["_fresh"] = True

    def test_every_employee_can_open_self_delegation_and_sees_only_allowed_levels(self):
        with self.app.test_client() as client:
            self._login(client)
            response = client.get("/delegation/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(self.same.email.encode(), response.data)
        self.assertIn(self.lower.email.encode(), response.data)
        self.assertNotIn(self.higher.email.encode(), response.data)
        self.assertNotIn(self.unassigned.email.encode(), response.data)

    def test_server_rejects_a_target_at_a_higher_or_unknown_level(self):
        with self.app.test_client() as client:
            self._login(client)
            higher_response = client.post(
                "/delegation/create",
                data={
                    "from_user_id": self.higher.id,
                    "to_user_id": self.higher.id,
                    "mode": "days",
                    "start_day": "2030-01-01",
                    "days_count": 1,
                },
            )
            unassigned_response = client.post(
                "/delegation/create",
                data={
                    "to_user_id": self.unassigned.id,
                    "mode": "days",
                    "start_day": "2030-01-01",
                    "days_count": 1,
                },
            )

        self.assertEqual(higher_response.status_code, 302)
        self.assertEqual(unassigned_response.status_code, 302)
        self.assertEqual(Delegation.query.count(), 0)

    def test_server_accepts_same_or_lower_level_and_forces_the_current_delegator(self):
        with self.app.test_client() as client:
            self._login(client)
            response = client.post(
                "/delegation/create",
                data={
                    "from_user_id": self.higher.id,
                    "to_user_id": self.lower.id,
                    "mode": "days",
                    "start_day": "2030-01-01",
                    "days_count": 1,
                },
            )

        self.assertEqual(response.status_code, 302)
        row = Delegation.query.one()
        self.assertEqual(row.from_user_id, self.actor.id)
        self.assertEqual(row.to_user_id, self.lower.id)

    def test_legacy_delegation_is_a_deliberate_single_identity_choice(self):
        now = datetime.now()
        grant = Delegation(
            from_user_id=self.higher.id,
            to_user_id=self.actor.id,
            starts_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=1),
            is_active=True,
            created_by_id=self.higher.id,
        )
        db.session.add(grant)
        db.session.commit()

        with self.app.test_client() as client:
            self._login(client)

            personal = client.get("/delegation/context").get_json()
            self.assertEqual(personal["working_user_id"], self.actor.id)
            self.assertIsNone(personal["legacy_delegation_id"])

            selected = client.post(f"/delegation/context/legacy/{grant.id}", data={"next": "/"})
            self.assertEqual(selected.status_code, 302)
            delegated = client.get("/delegation/context").get_json()
            self.assertEqual(delegated["actual_user_id"], self.actor.id)
            self.assertEqual(delegated["working_user_id"], self.higher.id)
            self.assertEqual(delegated["legacy_delegation_id"], grant.id)

            audit_response = client.post("/_test/delegation-audit")
            self.assertEqual(audit_response.status_code, 200)

            back_to_self = client.post("/delegation/context/self", data={"next": "/"})
            self.assertEqual(back_to_self.status_code, 302)
            personal_again = client.get("/delegation/context").get_json()
            self.assertEqual(personal_again["working_user_id"], self.actor.id)
            self.assertIsNone(personal_again["legacy_delegation_id"])

        audit = AuditLog.query.filter_by(action="TEST_DELEGATED_ACTION").one()
        self.assertEqual(audit.user_id, self.actor.id)
        self.assertEqual(audit.actual_user_id, self.actor.id)
        self.assertEqual(audit.on_behalf_of_id, self.higher.id)
        self.assertEqual(audit.acting_for_user_id, self.higher.id)
        self.assertEqual(audit.delegation_id, grant.id)

    def test_delegated_workflow_decision_uses_principal_public_identity(self):
        now = datetime.now()
        grant = Delegation(
            from_user_id=self.higher.id,
            to_user_id=self.actor.id,
            starts_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=1),
            is_active=True,
            created_by_id=self.higher.id,
        )
        request_row = WorkflowRequest(
            requester_id=self.lower.id,
            title="Delegated workflow decision",
            description="",
            status="IN_PROGRESS",
        )
        db.session.add_all((grant, request_row))
        db.session.flush()
        instance = WorkflowInstance(
            request_id=request_row.id,
            current_step_order=1,
            is_completed=False,
        )
        db.session.add(instance)
        db.session.flush()
        db.session.add(WorkflowInstanceStep(
            instance_id=instance.id,
            step_order=1,
            mode="SEQUENTIAL",
            approver_kind="USER",
            approver_user_id=self.higher.id,
            status="PENDING",
        ))
        db.session.commit()

        with self.app.test_client() as client:
            self._login(client)
            selected = client.post(f"/delegation/context/legacy/{grant.id}", data={"next": "/"})
            self.assertEqual(selected.status_code, 302)
            response = client.post(
                f"/workflow/request/{request_row.id}/step/1/decide",
                data={"decision": "APPROVED", "note": "delegated approval"},
            )

        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        step = WorkflowInstanceStep.query.filter_by(instance_id=instance.id, step_order=1).one()
        self.assertEqual(step.decided_by_id, self.higher.id)

        audit = AuditLog.query.filter_by(request_id=request_row.id, action="STEP_APPROVED").one()
        self.assertEqual(audit.user_id, self.actor.id)
        self.assertEqual(audit.actual_user_id, self.actor.id)
        self.assertEqual(audit.on_behalf_of_id, self.higher.id)
        self.assertEqual(audit.acting_for_user_id, self.higher.id)
        self.assertEqual(audit.delegation_id, grant.id)
        self.assertEqual(audit_display_actor(audit, self.lower).id, self.higher.id)
        self.assertNotIn(self.actor.email, audit_display_note(audit, self.lower))

        notification = Notification.query.filter_by(
            user_id=self.lower.id,
            type="WORKFLOW",
        ).order_by(Notification.id.desc()).first()
        self.assertIsNotNone(notification)
        self.assertIn(self.higher.full_name, notification.message)
        self.assertNotIn(self.actor.full_name, notification.message)

    def test_selected_identity_does_not_keep_delegatees_personal_permissions(self):
        now = datetime.now()
        grant = Delegation(
            from_user_id=self.higher.id,
            to_user_id=self.actor.id,
            starts_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=1),
            is_active=True,
            created_by_id=self.higher.id,
        )
        db.session.add_all((
            grant,
            UserPermission(user_id=self.actor.id, key="PORTAL_READ", is_allowed=True),
        ))
        db.session.commit()

        with self.app.test_client() as client:
            self._login(client)
            self.assertEqual(client.get("/_test/delegation-permission-subject").data, b"allowed")

            selected = client.post(f"/delegation/context/legacy/{grant.id}", data={"next": "/"})
            self.assertEqual(selected.status_code, 302)
            self.assertEqual(client.get("/_test/delegation-permission-subject").data, b"denied")

            personal = client.post("/delegation/context/self", data={"next": "/"})
            self.assertEqual(personal.status_code, 302)
            self.assertEqual(client.get("/_test/delegation-permission-subject").data, b"allowed")

    def test_delegated_events_are_published_with_the_principal_identity(self):
        now = datetime.now()
        grant = Delegation(
            from_user_id=self.higher.id,
            to_user_id=self.actor.id,
            starts_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=1),
            is_active=True,
            created_by_id=self.higher.id,
        )
        db.session.add(grant)
        db.session.commit()

        with self.app.test_client() as client:
            self._login(client)
            self.assertEqual(
                client.post(f"/delegation/context/legacy/{grant.id}", data={"next": "/"}).status_code,
                302,
            )
            response = client.post(
                "/_test/delegation-public-event",
                data={"recipient_id": self.lower.id},
            )

        self.assertEqual(response.status_code, 200)
        notification = Notification.query.filter_by(
            user_id=self.lower.id,
            type="INFO",
        ).order_by(Notification.id.desc()).first()
        self.assertIsNotNone(notification)
        self.assertEqual(notification.actor_id, self.higher.id)
        self.assertIn(self.higher.full_name, notification.message)
        self.assertNotIn(self.actor.full_name, notification.message)


if __name__ == "__main__":
    unittest.main()
