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
    InvItemCategory,
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
            DictLoader({
                "portal/inventory/base.html": "{% block inv_content %}{% endblock %}",
                "portal/layout.html": "{% block content %}{% endblock %}",
            }),
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
        self.tech_warehouse_manager = User(
            email="tech-warehouse@example.test",
            name="Technology Warehouse Manager",
            password_hash="x",
            role="employee",
        )
        self.tech_director = User(
            email="tech-director@example.test",
            name="Technology Director",
            password_hash="x",
            role="employee",
        )
        self.admin_maintenance_manager = User(
            email="admin-maintenance@example.test",
            name="Administrative Maintenance Manager",
            password_hash="x",
            role="employee",
        )
        self.admin_finance_director = User(
            email="admin-finance@example.test",
            name="Administrative Finance Director",
            password_hash="x",
            role="employee",
        )
        self.secretary_general = User(
            email="secretary-general@example.test",
            name="Secretary General",
            password_hash="x",
            role="employee",
        )
        db.session.add_all((
            self.employee,
            self.first_manager,
            self.second_manager,
            self.warehouse_manager,
            self.hr_director,
            self.tech_warehouse_manager,
            self.tech_director,
            self.admin_maintenance_manager,
            self.admin_finance_director,
            self.secretary_general,
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
            UserPermission(user_id=self.tech_warehouse_manager.id, key="INVENTORY_TECH_WAREHOUSE_APPROVE", is_allowed=True),
            UserPermission(user_id=self.tech_director.id, key="INVENTORY_TECH_DIRECTOR_APPROVE", is_allowed=True),
            UserPermission(user_id=self.admin_maintenance_manager.id, key="INVENTORY_ADMIN_MAINTENANCE_APPROVE", is_allowed=True),
            UserPermission(user_id=self.admin_finance_director.id, key="INVENTORY_ADMIN_FINANCE_DIRECTOR_APPROVE", is_allowed=True),
            UserPermission(user_id=self.secretary_general.id, key="INVENTORY_SECRETARY_GENERAL_APPROVE", is_allowed=True),
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

    def _special_route_item(self, *, category_name, item_name, item_code):
        category = InvItemCategory(name=category_name, is_active=True)
        db.session.add(category)
        db.session.flush()
        item = InvItem(
            name=item_name,
            code=item_code,
            category_id=category.id,
            is_active=True,
        )
        db.session.add(item)
        db.session.commit()
        return item

    def _add_stock(self, item, quantity=10, warehouse=None):
        warehouse = warehouse or self.warehouse
        voucher = InvStocktakeVoucher(
            voucher_no=f"STK-SPECIAL-{item.id}",
            voucher_date=date.today().isoformat(),
            warehouse_id=warehouse.id,
            created_by_id=self.hr_director.id,
        )
        db.session.add(voucher)
        db.session.flush()
        db.session.add(InvStocktakeVoucherLine(
            voucher_id=voucher.id,
            item_id=item.id,
            qty=quantity,
        ))
        db.session.commit()

    def test_legacy_manager_stage_is_routed_to_the_warehouse(self):
        self._login(self.second_manager.id)
        response = self.client.post(
            f"/portal/inventory/employee-requests/{self.request.id}/approve",
            data={"decision": "reject", "note": "Managers no longer approve materials"},
        )

        self.assertEqual(response.status_code, 403)

        self._login(self.warehouse_manager.id)
        response = self.client.post(
            f"/portal/inventory/employee-requests/{self.request.id}/approve",
            data={"decision": "reject", "note": "Warehouse review"},
        )

        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        row = db.session.get(InvEmployeeRequest, self.request.id)
        self.assertEqual(row.status, "REJECTED")
        action = InvEmployeeRequestAction.query.filter_by(
            request_id=row.id,
            stage="WAREHOUSE",
            action="REJECTED",
        ).one()
        self.assertEqual(action.actor_user_id, self.warehouse_manager.id)

    def test_new_request_bypasses_direct_manager_and_goes_to_warehouse(self):
        item = InvItem.query.filter_by(code="PAPER-001").one()
        self._login(self.employee.id)
        response = self.client.post(
            "/portal/inventory/employee-requests/new",
            data={
                "item_id": str(item.id),
                "requested_qty": "2",
                "purpose": "New office supply request",
            },
        )

        self.assertEqual(response.status_code, 302)
        row = InvEmployeeRequest.query.order_by(InvEmployeeRequest.id.desc()).first()
        self.assertEqual(row.approval_stage, "WAREHOUSE")
        self.assertIsNone(row.manager_user_id)
        self.assertIsNone(row.manager_user_ids)

        self._login(self.first_manager.id)
        manager_attempt = self.client.post(
            f"/portal/inventory/employee-requests/{row.id}/approve",
            data={"decision": "reject", "note": "Managers are bypassed"},
        )
        self.assertEqual(manager_attempt.status_code, 403)

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

    def test_warehouse_review_assigns_each_line_to_its_own_warehouse_in_one_stock_lookup(self):
        first_item = InvItem.query.filter_by(code="PAPER-001").one()
        second_item = InvItem(name="Ink cartridge", code="INK-001", is_active=True)
        second_warehouse = InvWarehouse(name="Technology warehouse", code="TECH", is_active=True)
        db.session.add_all((second_item, second_warehouse))
        db.session.commit()
        self._add_stock(first_item, quantity=10)
        self._add_stock(second_item, quantity=10, warehouse=second_warehouse)

        self._login(self.employee.id)
        created = self.client.post(
            "/portal/inventory/employee-requests/new",
            data={
                "item_id": [str(first_item.id), str(second_item.id)],
                "requested_qty": ["2", "3"],
                "purpose": "Request from separate warehouses",
            },
        )
        self.assertEqual(created.status_code, 302)
        row = InvEmployeeRequest.query.order_by(InvEmployeeRequest.id.desc()).first()
        lines = {line.item_id: line for line in row.lines}

        self._login(self.warehouse_manager.id)
        review = self.client.get(f"/portal/inventory/employee-requests/{row.id}")
        self.assertEqual(review.status_code, 200)
        review_markup = review.get_data(as_text=True)
        self.assertIn(f'name="warehouse_id_{lines[first_item.id].id}"', review_markup)
        self.assertIn(f'name="warehouse_id_{lines[second_item.id].id}"', review_markup)
        self.assertNotIn('name="warehouse_id" id="warehouseSelect"', review_markup)

        with patch(
            "portal.supply_requests._inventory_balances",
            wraps=_inv_build_balances,
        ) as build_balances:
            approved = self.client.post(
                f"/portal/inventory/employee-requests/{row.id}/approve",
                data={
                    "decision": "approve",
                    f"warehouse_id_{lines[first_item.id].id}": str(self.warehouse.id),
                    f"approved_qty_{lines[first_item.id].id}": "2",
                    f"warehouse_id_{lines[second_item.id].id}": str(second_warehouse.id),
                    f"approved_qty_{lines[second_item.id].id}": "3",
                },
            )

        self.assertEqual(approved.status_code, 302)
        self.assertEqual(build_balances.call_count, 1)
        self.assertEqual(
            set(build_balances.call_args.kwargs["warehouse_ids"]),
            {self.warehouse.id, second_warehouse.id},
        )
        db.session.expire_all()
        updated = db.session.get(InvEmployeeRequest, row.id)
        updated_lines = {line.item_id: line for line in updated.lines}
        self.assertEqual(updated.status, "APPROVED")
        self.assertEqual(updated_lines[first_item.id].warehouse_id, self.warehouse.id)
        self.assertEqual(updated_lines[second_item.id].warehouse_id, second_warehouse.id)
        self.assertEqual(InvIssueVoucher.query.count(), 2)
        self.assertEqual(
            {
                db.session.get(InvIssueVoucher, line.issue_voucher_id).from_warehouse_id
                for line in updated_lines.values()
            },
            {self.warehouse.id, second_warehouse.id},
        )

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
        self._login(self.warehouse_manager.id)

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
            stage="WAREHOUSE",
            action="REJECTED",
        ).one()
        self.assertEqual(action.actor_user_id, self.warehouse_manager.id)
        self.assertEqual(action.note, "The request is not needed")

    def test_request_list_filters_status_stage_employee_and_text(self):
        db.session.add(InvEmployeeRequest(
            requester_user_id=self.first_manager.id,
            items_text="Network cable",
            purpose="A different request",
            approval_stage="DONE",
            status="APPROVED",
        ))
        db.session.commit()

        self._login(self.warehouse_manager.id)
        with patch("portal.supply_requests.render_template", return_value="list") as render:
            response = self.client.get(
                "/portal/inventory/employee-requests?status=SUBMITTED&stage=WAREHOUSE"
                f"&user_id={self.employee.id}&q=A4"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row.id for row in render.call_args.kwargs["rows"]], [self.request.id])
        self.assertEqual(render.call_args.kwargs["filters"], {
            "q": "A4",
            "status": "SUBMITTED",
            "stage": "WAREHOUSE",
            "user_id": str(self.employee.id),
            "date_from": "",
            "date_to": "",
        })

    def test_request_list_page_renders_filter_controls(self):
        self._login(self.warehouse_manager.id)
        response = self.client.get("/portal/inventory/employee-requests")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('id="requestSearch"', body)
        self.assertIn('id="requestStatus"', body)
        self.assertIn('id="requestStage"', body)

    def test_request_templates_compile(self):
        for template_name in (
            "portal/inventory/employee_requests.html",
            "portal/inventory/employee_request_view.html",
            "portal/inventory/request_settings.html",
            "portal/inventory/admin_categories.html",
        ):
            with self.subTest(template=template_name):
                self.app.jinja_env.get_template(template_name)

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

    def test_request_view_loads_only_its_own_catalogue_items(self):
        item = InvItem.query.filter_by(code="PAPER-001").one()
        other_item = InvItem(name="Other material", code="OTHER-001", is_active=True)
        db.session.add_all((
            other_item,
            InvEmployeeRequestLine(
                request_id=self.request.id,
                item_id=item.id,
                requested_qty=2,
            ),
        ))
        db.session.commit()
        self._login(self.employee.id)

        with patch("portal.supply_requests.render_template", return_value="view") as render:
            response = self.client.get(
                f"/portal/inventory/employee-requests/{self.request.id}"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row.id for row in render.call_args.kwargs["items"]],
            [item.id],
        )
        self.assertEqual(render.call_args.kwargs["approval_stage"], "WAREHOUSE")
        self.assertEqual(
            render.call_args.kwargs["stage_responsibles"]["WAREHOUSE"],
            self.warehouse_manager.full_name,
        )
        self.assertEqual(
            render.call_args.kwargs["stage_responsibles"]["TECH_WAREHOUSE"],
            self.tech_warehouse_manager.full_name,
        )
        self.assertEqual(
            render.call_args.kwargs["stage_responsibles"]["ADMIN_MAINTENANCE"],
            self.admin_maintenance_manager.full_name,
        )
        self.assertEqual(
            render.call_args.kwargs["stage_responsibles"]["TECH_DIRECTOR"],
            self.tech_director.full_name,
        )
        self.assertEqual(
            render.call_args.kwargs["stage_responsibles"]["ADMIN_FINANCE"],
            self.admin_finance_director.full_name,
        )

    def test_action_log_displays_the_responsible_approver_name(self):
        action = InvEmployeeRequestAction.query.filter_by(request_id=self.request.id).one()
        self.request.approval_stage = "WAREHOUSE"
        action.stage = "WAREHOUSE"
        db.session.commit()
        self._login(self.employee.id)

        response = self.client.get(
            f"/portal/inventory/employee-requests/{self.request.id}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            f"مدير المستودع ({self.warehouse_manager.full_name})",
            response.get_data(as_text=True),
        )

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

    def test_technology_request_uses_its_dedicated_route_and_can_go_to_secretary_general(self):
        item = self._special_route_item(
            category_name="أجهزة حاسوب وتوابعها",
            item_name="Laptop",
            item_code="LAPTOP-001",
        )
        self._add_stock(item)
        self._login(self.employee.id)

        response = self.client.post(
            "/portal/inventory/employee-requests/new",
            data={
                "item_id": str(item.id),
                "requested_qty": "2",
                "purpose": "Technology request",
            },
        )

        self.assertEqual(response.status_code, 302)
        row = InvEmployeeRequest.query.order_by(InvEmployeeRequest.id.desc()).first()
        self.assertEqual(row.route_type, "TECH")
        self.assertEqual(row.approval_stage, "TECH_WAREHOUSE")

        self._login(self.warehouse_manager.id)
        self.assertEqual(
            self.client.post(
                f"/portal/inventory/employee-requests/{row.id}/approve",
                data={"decision": "approve"},
            ).status_code,
            403,
        )

        self._login(self.tech_warehouse_manager.id)
        prepared = self.client.post(
            f"/portal/inventory/employee-requests/{row.id}/approve",
            data={
                "decision": "approve",
                "warehouse_id": str(self.warehouse.id),
                f"approved_qty_{row.lines[0].id}": "2",
            },
        )
        self.assertEqual(prepared.status_code, 302)
        db.session.expire_all()
        row = db.session.get(InvEmployeeRequest, row.id)
        self.assertEqual(row.approval_stage, "TECH_DIRECTOR")
        self.assertEqual(InvIssueVoucher.query.count(), 0)

        self._login(self.tech_director.id)
        self.assertEqual(
            self.client.post(
                f"/portal/inventory/employee-requests/{row.id}/approve",
                data={"decision": "approve", "note": "Technology approved"},
            ).status_code,
            302,
        )
        db.session.expire_all()
        row = db.session.get(InvEmployeeRequest, row.id)
        self.assertEqual(row.approval_stage, "ADMIN_FINANCE")

        self._login(self.admin_finance_director.id)
        self.assertEqual(
            self.client.post(
                f"/portal/inventory/employee-requests/{row.id}/approve",
                data={"decision": "forward_secretary", "note": "Needs secretary decision"},
            ).status_code,
            302,
        )
        db.session.expire_all()
        row = db.session.get(InvEmployeeRequest, row.id)
        self.assertEqual(row.approval_stage, "SECRETARY_GENERAL")
        self.assertTrue(InvEmployeeRequestAction.query.filter_by(
            request_id=row.id,
            stage="ADMIN_FINANCE",
            action="FORWARDED",
        ).first())

        self._login(self.secretary_general.id)
        approved = self.client.post(
            f"/portal/inventory/employee-requests/{row.id}/approve",
            data={"decision": "approve", "note": "Secretary approved"},
        )
        self.assertEqual(approved.status_code, 302)
        db.session.expire_all()
        row = db.session.get(InvEmployeeRequest, row.id)
        self.assertEqual(row.status, "APPROVED")
        self.assertEqual(row.approval_stage, "DONE")
        self.assertEqual(InvIssueVoucher.query.count(), 1)

    def test_furniture_or_maintenance_route_can_end_with_director_final_approval(self):
        item = self._special_route_item(
            category_name="أثاث",
            item_name="Office desk",
            item_code="DESK-001",
        )
        self._add_stock(item)
        self._login(self.employee.id)

        response = self.client.post(
            "/portal/inventory/employee-requests/new",
            data={
                "item_id": str(item.id),
                "requested_qty": "1",
                "purpose": "Furniture request",
            },
        )
        self.assertEqual(response.status_code, 302)
        row = InvEmployeeRequest.query.order_by(InvEmployeeRequest.id.desc()).first()
        self.assertEqual(row.route_type, "ADMIN_MAINTENANCE")
        self.assertEqual(row.approval_stage, "ADMIN_MAINTENANCE")

        self._login(self.admin_maintenance_manager.id)
        prepared = self.client.post(
            f"/portal/inventory/employee-requests/{row.id}/approve",
            data={
                "decision": "approve",
                "warehouse_id": str(self.warehouse.id),
                f"approved_qty_{row.lines[0].id}": "1",
            },
        )
        self.assertEqual(prepared.status_code, 302)
        db.session.expire_all()
        row = db.session.get(InvEmployeeRequest, row.id)
        self.assertEqual(row.approval_stage, "ADMIN_FINANCE")
        self.assertEqual(InvIssueVoucher.query.count(), 0)

        self._login(self.admin_finance_director.id)
        approved = self.client.post(
            f"/portal/inventory/employee-requests/{row.id}/approve",
            data={"decision": "approve", "note": "Final director approval"},
        )
        self.assertEqual(approved.status_code, 302)
        db.session.expire_all()
        row = db.session.get(InvEmployeeRequest, row.id)
        self.assertEqual(row.status, "APPROVED")
        self.assertEqual(row.approval_stage, "DONE")
        self.assertEqual(InvIssueVoucher.query.count(), 1)

    def test_auto_categories_route_new_technology_and_maintenance_names(self):
        technology_item = self._special_route_item(
            category_name="ملحقات حاسوب",
            item_name="Computer accessory",
            item_code="ACCESSORY-001",
        )
        maintenance_item = self._special_route_item(
            category_name="صيانة تجهيزات",
            item_name="Equipment maintenance",
            item_code="MAINT-001",
        )
        self._login(self.employee.id)

        technology_response = self.client.post(
            "/portal/inventory/employee-requests/new",
            data={
                "item_id": str(technology_item.id),
                "requested_qty": "1",
                "purpose": "Technology accessory request",
            },
        )
        self.assertEqual(technology_response.status_code, 302)
        technology_request = InvEmployeeRequest.query.order_by(InvEmployeeRequest.id.desc()).first()
        self.assertEqual(technology_request.route_type, "TECH")
        self.assertEqual(technology_request.approval_stage, "TECH_WAREHOUSE")

        maintenance_response = self.client.post(
            "/portal/inventory/employee-requests/new",
            data={
                "item_id": str(maintenance_item.id),
                "requested_qty": "1",
                "purpose": "Maintenance request",
            },
        )
        self.assertEqual(maintenance_response.status_code, 302)
        maintenance_request = InvEmployeeRequest.query.order_by(InvEmployeeRequest.id.desc()).first()
        self.assertEqual(maintenance_request.route_type, "ADMIN_MAINTENANCE")
        self.assertEqual(maintenance_request.approval_stage, "ADMIN_MAINTENANCE")

    def test_a_request_must_not_mix_special_and_normal_routing_categories(self):
        item = self._special_route_item(
            category_name="أجهزة حاسوب وتوابعها",
            item_name="Laptop",
            item_code="LAPTOP-001",
        )
        regular_item = InvItem.query.filter_by(code="PAPER-001").one()
        self._login(self.employee.id)

        response = self.client.post(
            "/portal/inventory/employee-requests/new",
            data={
                "item_id": [str(item.id), str(regular_item.id)],
                "requested_qty": ["1", "1"],
                "purpose": "Mixed request",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(InvEmployeeRequest.query.count(), 1)


if __name__ == "__main__":
    unittest.main()
