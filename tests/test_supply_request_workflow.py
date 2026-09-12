import json
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from flask import Flask, g
from flask_login import LoginManager
from jinja2 import ChoiceLoader, DictLoader

from extensions import db
from models import InvEmployeeRequest, InvEmployeeRequestAction, InvItem, User, UserPermission
from portal import portal_bp


class SupplyRequestWorkflowTests(unittest.TestCase):
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
            WTF_CSRF_ENABLED=False,
            SECRET_KEY="supply-request-workflow-test",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(cls.app)
        login_manager = LoginManager()
        login_manager.init_app(cls.app)

        @login_manager.user_loader
        def load_user(user_id):
            return db.session.get(User, int(user_id))

        cls.app.register_blueprint(portal_bp)
        cls.app.jinja_loader = ChoiceLoader([
            DictLoader({"portal/inventory/base.html": "{% block inv_content %}{% endblock %}"}),
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
        self.employee = User(email="employee@example.test", name="Employee", password_hash="x", role="employee")
        self.first_manager = User(email="manager-1@example.test", name="Manager One", password_hash="x", role="employee")
        self.second_manager = User(email="manager-2@example.test", name="Manager Two", password_hash="x", role="employee")
        db.session.add_all((self.employee, self.first_manager, self.second_manager))
        db.session.flush()
        self.request = InvEmployeeRequest(
            requester_user_id=self.employee.id,
            manager_user_id=self.first_manager.id,
            manager_user_ids=json.dumps([self.first_manager.id, self.second_manager.id]),
            items_text="A4 paper",
            purpose="Office supply",
            approval_stage="MANAGER",
            status="SUBMITTED",
        )
        db.session.add(self.request)
        db.session.flush()
        db.session.add(InvEmployeeRequestAction(
            request_id=self.request.id,
            stage="MANAGER",
            action="SUBMITTED",
            actor_user_id=self.employee.id,
            note="Submitted",
            created_at=datetime.utcnow(),
        ))
        db.session.add_all((
            InvItem(name="Printer paper", code="PAPER-001", is_active=True),
            UserPermission(user_id=self.employee.id, key="PORTAL_READ", is_allowed=True),
        ))
        db.session.commit()
        self.client = self.app.test_client()

    def _login(self, user_id):
        for key in ("_login_user", "_role_permission_keys"):
            g.pop(key, None)
        with self.client.session_transaction() as session:
            session.clear()
            session["_user_id"] = str(user_id)
            session["_fresh"] = True

    def test_one_of_the_direct_managers_can_approve_the_request(self):
        self._login(self.second_manager.id)
        response = self.client.post(
            f"/portal/inventory/employee-requests/{self.request.id}/approve",
            data={"note": "Reviewed by the second manager"},
        )

        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        row = db.session.get(InvEmployeeRequest, self.request.id)
        self.assertEqual(row.approval_stage, "WAREHOUSE")
        action = InvEmployeeRequestAction.query.filter_by(
            request_id=row.id,
            stage="MANAGER",
            action="APPROVED",
        ).one()
        self.assertEqual(action.actor_user_id, self.second_manager.id)

        self._login(self.first_manager.id)
        duplicate = self.client.post(
            f"/portal/inventory/employee-requests/{row.id}/approve",
            data={"note": "A second decision must not be accepted"},
        )
        self.assertEqual(duplicate.status_code, 403)

    def test_requester_can_download_the_official_pdf_and_word_forms(self):
        self._login(self.employee.id)

        pdf_response = self.client.get(
            f"/portal/inventory/employee-requests/{self.request.id}/form.pdf"
        )
        word_response = self.client.get(
            f"/portal/inventory/employee-requests/{self.request.id}/form.docx"
        )

        self.assertEqual(pdf_response.status_code, 200)
        self.assertEqual(pdf_response.mimetype, "application/pdf")
        self.assertEqual(word_response.status_code, 200)
        self.assertIn("wordprocessingml", word_response.mimetype)

    def test_requester_can_search_the_catalogue_from_the_material_request_form(self):
        self._login(self.employee.id)

        initial = self.client.get("/portal/inventory/items/search.json")
        matched = self.client.get("/portal/inventory/items/search.json?q=paper")
        with patch("portal.supply_requests.render_template", return_value="form") as render:
            form = self.client.get("/portal/inventory/employee-requests/new")

        self.assertEqual(initial.status_code, 200)
        self.assertEqual(matched.status_code, 200)
        self.assertEqual(initial.get_json()["items"][0]["code"], "PAPER-001")
        self.assertEqual(matched.get_json()["items"][0]["label"], "Printer paper (PAPER-001)")
        self.assertEqual(form.status_code, 200)
        self.assertTrue(render.call_args.kwargs["catalog_has_items"])
        self.assertEqual(render.call_args.kwargs["items"], [])


if __name__ == "__main__":
    unittest.main()
