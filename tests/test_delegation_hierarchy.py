import unittest
from pathlib import Path

from flask import Flask, g
from flask_login import LoginManager
from jinja2 import ChoiceLoader, DictLoader

from delegation import delegation_bp
from extensions import db
from models import Delegation, OrgNode, OrgNodeAssignment, OrgNodeType, User


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


if __name__ == "__main__":
    unittest.main()
