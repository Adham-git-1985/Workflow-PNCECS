import json
import unittest
from urllib.parse import unquote
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from flask import Flask, g
from flask_login import LoginManager
from jinja2 import ChoiceLoader, DictLoader

from extensions import db
from models import (
    InvEmployeeRequest,
    InvEmployeeRequestAction,
    InvEmployeeRequestLine,
    InvIssueVoucher,
    InvReturnVoucher,
    InvReturnVoucherLine,
    InvItem,
    InvStocktakeVoucher,
    InvStocktakeVoucherLine,
    InvWarehouse,
    User,
    UserPermission,
)
from portal import portal_bp
from portal.routes import _inv_build_balances


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
        self.warehouse_manager = User(
            email="warehouse-manager@example.test",
            name="Warehouse Manager",
            password_hash="x",
            role="employee",
        )
        self.hr_director = User(
            email="hr-director@example.test",
            name="HR Director",
            password_hash="x",
            role="HR_MANAGER",
        )
        db.session.add_all((
            self.employee,
            self.first_manager,
            self.second_manager,
            self.warehouse_manager,
            self.hr_director,
        ))
        db.session.flush()
        self.warehouse = InvWarehouse(name="Main warehouse", code="MAIN", is_active=True)
        db.session.add(self.warehouse)
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
            UserPermission(user_id=self.warehouse_manager.id, key="INVENTORY_REQUEST_APPROVE", is_allowed=True),
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

    def _submit_warehouse_manager_request(self):
        item = InvItem.query.filter_by(code="PAPER-001").one()
        self._login(self.warehouse_manager.id)
        response = self.client.post(
            "/portal/inventory/employee-requests/new",
            data={
                "item_id": str(item.id),
                "requested_qty": "3",
                "purpose": "Warehouse manager request",
            },
        )
        self.assertEqual(response.status_code, 302)
        return InvEmployeeRequest.query.order_by(InvEmployeeRequest.id.desc()).first()

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

    def test_warehouse_manager_request_uses_hr_fallback_and_not_self_approval(self):
        row = self._submit_warehouse_manager_request()

        self.assertEqual(row.requester_user_id, self.warehouse_manager.id)
        self.assertEqual(row.approval_stage, "HR")

        self._login(self.warehouse_manager.id)
        self.assertEqual(
            self.client.post(
                f"/portal/inventory/employee-requests/{row.id}/approve",
                data={"decision": "approve"},
            ).status_code,
            403,
        )

    def test_hr_fallback_creates_employee_issue_voucher(self):
        row = self._submit_warehouse_manager_request()
        line = row.lines[0]
        stocktake = InvStocktakeVoucher(
            voucher_no="STK-TEST-001",
            voucher_date="2020-01-01",
            warehouse_id=self.warehouse.id,
            created_by_id=self.hr_director.id,
        )
        db.session.add(stocktake)
        db.session.flush()
        db.session.add(InvStocktakeVoucherLine(
            voucher_id=stocktake.id,
            item_id=line.item_id,
            qty=10,
        ))
        db.session.commit()

        self._login(self.hr_director.id)
        response = self.client.post(
            f"/portal/inventory/employee-requests/{row.id}/approve",
            data={
                "decision": "approve",
                "warehouse_id": str(self.warehouse.id),
                f"approved_qty_{line.id}": "3",
            },
        )

        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        updated = db.session.get(InvEmployeeRequest, row.id)
        self.assertEqual(updated.status, "APPROVED")
        self.assertEqual(updated.approval_stage, "DONE")
        voucher = InvIssueVoucher.query.one()
        self.assertEqual(voucher.issue_kind, "EMPLOYEE")
        self.assertEqual(voucher.from_warehouse_id, self.warehouse.id)
        self.assertEqual(updated.lines[0].issue_voucher_id, voucher.id)

    def test_same_day_issue_is_deducted_from_the_opening_balance(self):
        row = self._submit_warehouse_manager_request()
        line = row.lines[0]
        stocktake = InvStocktakeVoucher(
            voucher_no="STK-SAME-DAY",
            voucher_date=date.today().isoformat(),
            warehouse_id=self.warehouse.id,
            created_by_id=self.hr_director.id,
            created_at=datetime.utcnow() - timedelta(minutes=1),
        )
        db.session.add(stocktake)
        db.session.flush()
        db.session.add(InvStocktakeVoucherLine(
            voucher_id=stocktake.id,
            item_id=line.item_id,
            qty=10,
        ))
        db.session.commit()

        self._login(self.hr_director.id)
        response = self.client.post(
            f"/portal/inventory/employee-requests/{row.id}/approve",
            data={
                "decision": "approve",
                "warehouse_id": str(self.warehouse.id),
                f"approved_qty_{line.id}": "3",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(_inv_build_balances()[(self.warehouse.id, line.item_id)], 7.0)

    def test_full_employee_return_reverses_stock_and_removes_month_marker(self):
        row = self._submit_warehouse_manager_request()
        line = row.lines[0]
        stocktake = InvStocktakeVoucher(
            voucher_no="STK-RETURN-TEST",
            voucher_date="2020-01-01",
            warehouse_id=self.warehouse.id,
            created_by_id=self.hr_director.id,
        )
        db.session.add(stocktake)
        db.session.flush()
        db.session.add(InvStocktakeVoucherLine(
            voucher_id=stocktake.id,
            item_id=line.item_id,
            qty=10,
        ))
        db.session.commit()

        self._login(self.hr_director.id)
        approved = self.client.post(
            f"/portal/inventory/employee-requests/{row.id}/approve",
            data={
                "decision": "approve",
                "warehouse_id": str(self.warehouse.id),
                f"approved_qty_{line.id}": "3",
            },
        )
        self.assertEqual(approved.status_code, 302)
        self.assertEqual(_inv_build_balances()[(self.warehouse.id, line.item_id)], 7.0)

        return_page = self.client.get(f"/portal/inventory/employee-requests/{row.id}/return")
        self.assertEqual(return_page.status_code, 200)
        self.assertIn("لا يلزم إدخال اسم غرفة", return_page.get_data(as_text=True))

        return_data = {
            "voucher_date": date.today().isoformat(),
            f"return_qty_{line.id}": "3",
        }
        returned = self.client.post(
            f"/portal/inventory/employee-requests/{row.id}/return",
            data=return_data,
        )
        self.assertEqual(returned.status_code, 302)
        db.session.expire_all()
        updated = db.session.get(InvEmployeeRequest, row.id)
        self.assertEqual(updated.status, "RETURNED")
        voucher = InvReturnVoucher.query.one()
        self.assertEqual(voucher.source_request_id, row.id)
        self.assertEqual(voucher.to_warehouse_id, self.warehouse.id)
        return_line = InvReturnVoucherLine.query.one()
        self.assertEqual(return_line.source_request_line_id, line.id)
        self.assertEqual(return_line.qty, 3.0)
        self.assertEqual(_inv_build_balances()[(self.warehouse.id, line.item_id)], 10.0)

        self._login(self.warehouse_manager.id)
        marker = self.client.get(
            "/portal/inventory/employee-requests/items/search.json?q=paper"
        ).get_json()["items"][0]["last_request"]
        self.assertIsNone(marker)

    def test_current_approver_can_reject_with_a_recorded_reason(self):
        self._login(self.second_manager.id)

        response = self.client.post(
            f"/portal/inventory/employee-requests/{self.request.id}/approve",
            data={"decision": "reject", "note": "The request is not needed"},
        )

        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        row = db.session.get(InvEmployeeRequest, self.request.id)
        self.assertEqual(row.status, "REJECTED")
        action = InvEmployeeRequestAction.query.filter_by(
            request_id=row.id,
            stage="MANAGER",
            action="REJECTED",
        ).one()
        self.assertEqual(action.actor_user_id, self.second_manager.id)
        self.assertEqual(action.note, "The request is not needed")

    def test_requester_can_cancel_a_pending_material_request(self):
        self._login(self.employee.id)

        response = self.client.post(
            f"/portal/inventory/employee-requests/{self.request.id}/cancel",
            data={"note": "No longer required"},
        )

        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        row = db.session.get(InvEmployeeRequest, self.request.id)
        self.assertEqual(row.status, "CANCELLED")
        action = InvEmployeeRequestAction.query.filter_by(
            request_id=row.id,
            action="CANCELLED",
        ).one()
        self.assertEqual(action.actor_user_id, self.employee.id)

        self._login(self.first_manager.id)
        duplicate = self.client.post(f"/portal/inventory/employee-requests/{row.id}/cancel")
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
        employee_name = self.employee.full_name or self.employee.name or self.employee.email
        self.assertIn(f"{employee_name} - طلب المواد.pdf", unquote(pdf_response.headers["Content-Disposition"]))
        self.assertIn(f"{employee_name} - طلب المواد.docx", unquote(word_response.headers["Content-Disposition"]))

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

    def test_material_search_shows_the_employee_last_request_and_month_marker(self):
        item = InvItem.query.filter_by(code="PAPER-001").one()
        self.request.created_at = datetime.utcnow() - timedelta(days=12)
        db.session.add(InvEmployeeRequestLine(
            request_id=self.request.id,
            item_id=item.id,
            requested_qty=3,
        ))
        db.session.commit()
        self._login(self.employee.id)

        response = self.client.get(
            "/portal/inventory/employee-requests/items/search.json?q=paper"
        )

        self.assertEqual(response.status_code, 200)
        result = response.get_json()["items"][0]
        self.assertEqual(result["id"], item.id)
        self.assertEqual(result["last_request"]["request_id"], self.request.id)
        self.assertEqual(result["last_request"]["requested_qty"], 3.0)
        self.assertTrue(result["last_request"]["within_month"])
        self.assertIn("أقل من شهر", result["last_request"]["label"])

        self.request.created_at = datetime.utcnow() - timedelta(days=45)
        db.session.commit()
        older = self.client.get(
            "/portal/inventory/employee-requests/items/search.json?q=paper"
        ).get_json()["items"][0]["last_request"]
        self.assertFalse(older["within_month"])
        self.assertIn("أكثر من شهر", older["label"])


if __name__ == "__main__":
    unittest.main()
