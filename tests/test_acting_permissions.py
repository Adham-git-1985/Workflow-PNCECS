import unittest
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask
from flask_login import LoginManager
from jinja2 import ChoiceLoader, DictLoader

from delegation import delegation_bp
from extensions import db
from models import ActingPermission, FormalDelegation, Notification, User
from utils.acting_authorization import (
    ACTING,
    DELEGATED,
    EXPIRED,
    AuthorizationError,
    authorize_action,
    expire_due_records,
)


class ActingPermissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.app = Flask(__name__, template_folder=str(root / "templates"))
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="acting-permissions-test",
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
        db.drop_all()
        cls.context.pop()

    def setUp(self):
        db.session.remove()
        db.drop_all()
        db.create_all()
        self.principal = User(
            email="principal@example.test",
            name="Principal",
            password_hash="x",
            role="EMPLOYEE",
        )
        self.actor = User(
            email="actor@example.test",
            name="Actor",
            password_hash="x",
            role="EMPLOYEE",
        )
        db.session.add_all((self.principal, self.actor))
        db.session.commit()

    def _login(self, client):
        with client.session_transaction() as session:
            session.clear()
            session["_user_id"] = str(self.actor.id)
            session["_fresh"] = True

    def _window(self):
        now = datetime.utcnow()
        return now - timedelta(minutes=1), now + timedelta(hours=1)

    def test_acting_permission_is_scoped_and_does_not_approve(self):
        start_at, end_at = self._window()
        row = ActingPermission(
            principal_user_id=self.principal.id,
            acting_user_id=self.actor.id,
            module_id="WORKFLOW",
            scope_type="ALL",
            can_view=True,
            can_reject=True,
            can_approve=True,
            start_at=start_at,
            end_at=end_at,
            status="ACTIVE",
        )
        db.session.add(row)
        db.session.commit()

        with self.app.test_request_context("/"):
            from flask_login import login_user

            login_user(self.actor)
            with self.assertRaises(AuthorizationError):
                authorize_action(
                    "APPROVE",
                    module_id="WORKFLOW",
                    acting_permission=row,
                )
            decision = authorize_action(
                "REJECT",
                module_id="WORKFLOW",
                acting_permission=row,
            )
            self.assertEqual(decision.execution_context, ACTING)
            self.assertEqual(decision.actual_user_id, self.actor.id)
            self.assertEqual(decision.acting_for_user_id, self.principal.id)

    def test_formal_delegation_requires_reference_and_matches_type(self):
        start_at, end_at = self._window()
        row = FormalDelegation(
            delegator_user_id=self.principal.id,
            delegate_user_id=self.actor.id,
            delegation_type="APPROVAL",
            module_id="WORKFLOW",
            legal_reference="قرار 15/2026",
            start_at=start_at,
            end_at=end_at,
            status="ACTIVE",
        )
        db.session.add(row)
        db.session.commit()

        with self.app.test_request_context("/"):
            from flask_login import login_user

            login_user(self.actor)
            decision = authorize_action(
                "APPROVE",
                module_id="WORKFLOW",
                formal_delegation=row,
            )
            self.assertEqual(decision.execution_context, DELEGATED)
            with self.assertRaises(AuthorizationError):
                authorize_action(
                    "REJECT",
                    module_id="WORKFLOW",
                    formal_delegation=row,
                )

    def test_consent_is_a_formal_action(self):
        start_at, end_at = self._window()
        row = FormalDelegation(
            delegator_user_id=self.principal.id,
            delegate_user_id=self.actor.id,
            delegation_type="CONSENT",
            module_id="WORKFLOW",
            decision_number="15/2026",
            start_at=start_at,
            end_at=end_at,
            status="ACTIVE",
        )
        db.session.add(row)
        db.session.commit()

        with self.app.test_request_context("/"):
            from flask_login import login_user

            login_user(self.actor)
            decision = authorize_action(
                "CONSENT",
                module_id="WORKFLOW",
                formal_delegation=row,
            )
            self.assertEqual(decision.execution_context, DELEGATED)

    def test_expiry_is_persisted_and_notifies_the_recipient(self):
        now = datetime.utcnow()
        acting = ActingPermission(
            principal_user_id=self.principal.id,
            acting_user_id=self.actor.id,
            can_view=True,
            start_at=now - timedelta(hours=2),
            end_at=now - timedelta(hours=1),
            status="ACTIVE",
        )
        formal = FormalDelegation(
            delegator_user_id=self.principal.id,
            delegate_user_id=self.actor.id,
            delegation_type="APPROVAL",
            decision_number="15/2026",
            start_at=now - timedelta(hours=2),
            end_at=now - timedelta(hours=1),
            status="ACTIVE",
        )
        db.session.add_all((acting, formal))
        db.session.commit()

        changed = expire_due_records(now=now, auto_commit=True)
        self.assertEqual(changed, {"acting_permissions": 1, "formal_delegations": 1})
        self.assertEqual(db.session.get(ActingPermission, acting.id).status, EXPIRED)
        self.assertEqual(db.session.get(FormalDelegation, formal.id).status, EXPIRED)
        self.assertEqual(Notification.query.filter_by(user_id=self.actor.id).count(), 2)

    def test_dashboard_can_select_context_without_changing_login_identity(self):
        start_at, end_at = self._window()
        row = ActingPermission(
            principal_user_id=self.principal.id,
            acting_user_id=self.actor.id,
            can_view=True,
            start_at=start_at,
            end_at=end_at,
            status="ACTIVE",
        )
        db.session.add(row)
        db.session.commit()

        with self.app.test_client() as client:
            self._login(client)
            response = client.get("/delegation/permissions")
            self.assertEqual(response.status_code, 200)
            response = client.post(f"/delegation/context/acting/{row.id}")
            self.assertEqual(response.status_code, 302)
            context = client.get("/delegation/context").get_json()

        self.assertEqual(context["actual_user_id"], self.actor.id)
        self.assertEqual(context["acting_for_user_id"], self.principal.id)
        self.assertEqual(context["acting_permission_id"], row.id)
        self.assertEqual(context["execution_context"], ACTING)
        self.assertEqual(Notification.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
