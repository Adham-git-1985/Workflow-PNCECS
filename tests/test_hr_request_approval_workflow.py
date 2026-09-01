import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, g
from flask_login import LoginManager
from jinja2 import ChoiceLoader, DictLoader

from extensions import db
from models import (
    Delegation,
    Directorate,
    EmployeeFile,
    EmployeeSecondment,
    HRLeaveRequest,
    HRLeaveType,
    HRPermissionRequest,
    HRPermissionType,
    HRRequestObserver,
    Notification,
    Organization,
    OrgNode,
    OrgNodeAssignment,
    OrgNodeManager,
    OrgNodeType,
    OrgUnitManager,
    User,
    UserPermission,
)
from portal import portal_bp
from services.hr_request_workflow import (
    KIND_LEAVE,
    KIND_PERMISSION,
    board_visible_user_ids,
    can_user_act,
    current_step,
    decide_request,
    process_pending_approvals,
    start_request_flow,
)


class HRRequestApprovalWorkflowTests(unittest.TestCase):
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
            SECRET_KEY="hr-request-workflow-test",
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

        cls.app.register_blueprint(portal_bp)
        cls.app.add_url_rule(
            "/help/leaves-guide",
            endpoint="users.help_leaves_guide",
            view_func=lambda: "",
        )
        cls.app.jinja_loader = ChoiceLoader([
            DictLoader({"portal/layout.html": "{% block content %}{% endblock %}"}),
            cls.app.jinja_loader,
        ])
        cls.app.jinja_env.globals["csrf_token"] = lambda: "test-token"
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
        self.manager = User(email="manager@example.test", name="Manager", password_hash="x", role="dept_head")
        self.general_director = User(email="director@example.test", name="General Director", password_hash="x", role="directorate_head")
        self.hr = User(email="hr@example.test", name="HR Manager", password_hash="x", role="HR")
        self.secretary = User(email="secretary@example.test", name="Secretary General", password_hash="x", role="General_secretary")
        db.session.add_all((self.employee, self.manager, self.general_director, self.hr, self.secretary))
        db.session.flush()

        organization = Organization(name_ar="Organization", code="ORG")
        db.session.add(organization)
        db.session.flush()
        self.directorate = Directorate(organization_id=organization.id, name_ar="General Directorate", code="DIR")
        db.session.add(self.directorate)
        db.session.flush()
        self.employee.directorate_id = self.directorate.id
        db.session.add(EmployeeFile(user_id=self.employee.id, direct_manager_user_id=self.manager.id, directorate_id=self.directorate.id))
        db.session.add(OrgUnitManager(
            unit_type="DIRECTORATE",
            unit_id=self.directorate.id,
            manager_user_id=self.general_director.id,
        ))

        self.normal_type = HRLeaveType(code="ANNUAL", name_ar="Annual", requires_approval=True, is_active=True)
        self.external_type = HRLeaveType(code="EXTERNAL", name_ar="External", requires_approval=True, is_external=True, is_active=True)
        self.permission_type = HRPermissionType(code="PRIVATE", name_ar="Private", requires_approval=True, is_active=True)
        db.session.add_all((self.normal_type, self.external_type, self.permission_type))
        db.session.add_all([
            UserPermission(user_id=self.general_director.id, key=key, is_allowed=True)
            for key in ("PORTAL_READ", "HR_ABSENCE_BOARD_VIEW", "HR_REPORTS_EXPORT")
        ])
        db.session.add_all([
            UserPermission(user_id=self.manager.id, key=key, is_allowed=True)
            for key in ("PORTAL_READ", "HR_READ")
        ])
        db.session.commit()

    def _login(self, client, user_id):
        g.pop("_login_user", None)
        with client.session_transaction() as session:
            session.clear()
            session["_user_id"] = str(user_id)
            session["_fresh"] = True

    def _leave(self, leave_type):
        row = HRLeaveRequest(
            user_id=self.employee.id,
            leave_type_id=leave_type.id,
            start_date="2026-09-01",
            end_date="2026-09-02",
            days=2,
            status="SUBMITTED",
            submitted_at=datetime.utcnow(),
        )
        db.session.add(row)
        db.session.flush()
        return row

    def _permission(self, user=None):
        row = HRPermissionRequest(
            user_id=(user or self.employee).id,
            permission_type_id=self.permission_type.id,
            day="2026-09-01",
            from_time="10:00",
            to_time="11:00",
            status="SUBMITTED",
            submitted_at=datetime.utcnow(),
        )
        db.session.add(row)
        db.session.flush()
        return row

    def test_normal_leave_is_final_after_direct_manager_and_creates_cc(self):
        row = self._leave(self.normal_type)
        steps = start_request_flow(KIND_LEAVE, row)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].approver_user_id, self.manager.id)

        result = decide_request(KIND_LEAVE, row, self.manager, "APPROVE", "ok")
        db.session.commit()

        self.assertEqual(result, "APPROVED")
        self.assertEqual(row.status, "APPROVED")
        observer_ids = {observer.user_id for observer in HRRequestObserver.query.filter_by(request_kind=KIND_LEAVE, request_id=row.id).all()}
        self.assertTrue({self.hr.id, self.general_director.id, self.secretary.id}.issubset(observer_ids))
        cc_ids = {
            notification.user_id
            for notification in Notification.query.filter_by(type="HR_REQUEST_CC").all()
        }
        self.assertIn(self.hr.id, cc_ids)
        self.assertNotIn(self.general_director.id, cc_ids)
        self.assertNotIn(self.secretary.id, cc_ids)

    def test_leave_goes_to_all_hierarchy_managers_and_one_approval_is_enough(self):
        self.manager.name = "خلود"
        irene = User(email="irene@example.test", name="إيرين", password_hash="x", role="dept_head")
        raed = User(email="raed@example.test", name="رائد", password_hash="x", role="dept_head")
        node_type = OrgNodeType(code="LEAVE_BRANCH", name_ar="فرع الإجازة", sort_order=1)
        db.session.add_all((irene, raed, node_type))
        db.session.flush()

        nodes = [
            OrgNode(type_id=node_type.id, name_ar=name, is_active=True)
            for name in ("الفرع الأول", "الفرع الثاني", "الفرع الثالث")
        ]
        db.session.add_all(nodes)
        db.session.flush()
        self.employee.org_node_id = nodes[0].id
        db.session.add_all([
            OrgNodeAssignment(user_id=self.employee.id, node_id=nodes[0].id, is_primary=True),
            OrgNodeAssignment(user_id=self.employee.id, node_id=nodes[1].id, is_primary=False),
            OrgNodeAssignment(user_id=self.employee.id, node_id=nodes[2].id, is_primary=False),
            OrgNodeManager(node_id=nodes[0].id, manager_user_id=self.manager.id),
            OrgNodeManager(node_id=nodes[1].id, manager_user_id=irene.id),
            OrgNodeManager(node_id=nodes[2].id, manager_user_id=raed.id),
        ])
        for user in (self.employee, irene, raed):
            db.session.add_all([
                UserPermission(user_id=user.id, key="PORTAL_READ", is_allowed=True),
                UserPermission(user_id=user.id, key="HR_READ", is_allowed=True),
            ])
        db.session.add(UserPermission(
            user_id=self.employee.id,
            key="HR_REQUESTS_READ",
            is_allowed=True,
        ))
        db.session.commit()

        row = self._leave(self.normal_type)
        steps = start_request_flow(KIND_LEAVE, row)
        db.session.commit()

        expected_ids = [self.manager.id, irene.id, raed.id]
        self.assertEqual(len(steps), 1)
        self.assertEqual(json.loads(steps[0].approver_user_ids), expected_ids)
        self.assertEqual(steps[0].approver_user_id, self.manager.id)
        self.assertTrue(all(can_user_act(user, steps[0]) for user in (self.manager, irene, raed)))
        notified_ids = {
            notification.user_id
            for notification in Notification.query.filter_by(
                type="HR_APPROVAL",
                link_url=f"/portal/hr/approvals/leaves/{row.id}",
            ).all()
        }
        self.assertEqual(notified_ids, set(expected_ids))

        self.assertEqual(decide_request(KIND_LEAVE, row, irene, "APPROVE"), "APPROVED")
        db.session.commit()
        self.assertEqual(row.status, "APPROVED")
        self.assertEqual(steps[0].decided_by_id, irene.id)
        self.assertFalse(can_user_act(self.manager, steps[0]))
        self.assertFalse(can_user_act(raed, steps[0]))

        client = self.app.test_client()
        self._login(client, self.employee.id)
        my_leaves = client.get("/portal/hr/me/leaves")
        self.assertEqual(my_leaves.status_code, 200)
        for name in ("خلود", "إيرين", "رائد"):
            self.assertIn(name.encode("utf-8"), my_leaves.data)

        self._login(client, raed.id)
        history = client.get("/portal/hr/approvals?status=APPROVED")
        self.assertEqual(history.status_code, 200)
        self.assertIn(b"Employee", history.data)

    def test_leave_includes_active_secondment_manager_only(self):
        active_manager = User(
            email="active-secondment@example.test",
            name="Active Secondment Manager",
            password_hash="x",
            role="dept_head",
        )
        expired_manager = User(
            email="expired-secondment@example.test",
            name="Expired Secondment Manager",
            password_hash="x",
            role="dept_head",
        )
        db.session.add_all((active_manager, expired_manager))
        db.session.flush()
        db.session.add_all((
            EmployeeSecondment(
                user_id=self.employee.id,
                date_from="2026-01-01",
                date_to="2026-12-31",
                direct_manager_user_id=active_manager.id,
            ),
            EmployeeSecondment(
                user_id=self.employee.id,
                date_from="2025-01-01",
                date_to="2025-12-31",
                direct_manager_user_id=expired_manager.id,
            ),
        ))
        db.session.commit()

        row = self._leave(self.normal_type)
        steps = start_request_flow(KIND_LEAVE, row)

        self.assertEqual(len(steps), 1)
        self.assertEqual(
            json.loads(steps[0].approver_user_ids),
            [self.manager.id, active_manager.id],
        )
        self.assertTrue(can_user_act(self.manager, steps[0]))
        self.assertTrue(can_user_act(active_manager, steps[0]))
        self.assertFalse(can_user_act(expired_manager, steps[0]))

    def test_external_leave_requires_manager_then_hr_then_secretary_general(self):
        row = self._leave(self.external_type)
        steps = start_request_flow(KIND_LEAVE, row)
        self.assertEqual([step.stage_code for step in steps], ["DIRECT_MANAGER", "HR", "SECRETARY_GENERAL"])

        self.assertEqual(decide_request(KIND_LEAVE, row, self.manager, "APPROVE"), "NEXT")
        self.assertEqual(row.status, "SUBMITTED")
        self.assertEqual(current_step(KIND_LEAVE, row.id).stage_code, "HR")
        self.assertTrue(can_user_act(self.hr, current_step(KIND_LEAVE, row.id)))
        self.assertFalse(can_user_act(self.secretary, current_step(KIND_LEAVE, row.id)))

        self.assertEqual(decide_request(KIND_LEAVE, row, self.hr, "APPROVE"), "NEXT")
        self.assertEqual(current_step(KIND_LEAVE, row.id).stage_code, "SECRETARY_GENERAL")
        self.assertEqual(decide_request(KIND_LEAVE, row, self.secretary, "APPROVE"), "APPROVED")
        self.assertEqual(row.status, "APPROVED")

    def test_overdue_manager_step_escalates_without_auto_approval(self):
        row = self._leave(self.normal_type)
        start_request_flow(KIND_LEAVE, row)
        step = current_step(KIND_LEAVE, row.id)
        step.due_at = datetime.utcnow() - timedelta(minutes=1)
        db.session.flush()

        result = process_pending_approvals(now=datetime.utcnow(), send_notifications=False)

        self.assertEqual(result["escalated"], 1)
        self.assertEqual(row.status, "SUBMITTED")
        self.assertEqual(step.status, "PENDING")
        self.assertEqual(step.approver_user_id, self.general_director.id)
        self.assertEqual(step.escalation_reason, "GENERAL_DIRECTOR")

    def test_active_delegation_is_used_when_the_request_is_submitted(self):
        delegate = User(email="delegate@example.test", name="Delegate", password_hash="x", role="employee")
        db.session.add(delegate)
        db.session.flush()
        now = datetime.utcnow()
        db.session.add(Delegation(
            from_user_id=self.manager.id,
            to_user_id=delegate.id,
            starts_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=1),
            is_active=True,
        ))
        row = self._leave(self.normal_type)
        steps = start_request_flow(KIND_LEAVE, row, now=now)

        self.assertEqual(steps[0].approver_user_id, delegate.id)
        self.assertEqual(steps[0].escalation_reason, "ACTIVE_DELEGATION")

    def test_legacy_submitted_request_gets_a_flow_before_alert_processing(self):
        row = self._leave(self.normal_type)
        db.session.commit()

        result = process_pending_approvals(now=datetime.utcnow(), send_notifications=False)

        self.assertEqual(result["initialized"], 1)
        step = current_step(KIND_LEAVE, row.id)
        self.assertIsNotNone(step)
        self.assertEqual(step.approver_user_id, self.manager.id)

    def test_permission_goes_to_all_hierarchy_managers_and_one_approval_is_enough(self):
        requester = User(
            email="permission-requester@example.test",
            name="Permission Requester",
            password_hash="x",
            role="employee",
        )
        irene = User(email="permission-irene@example.test", name="Irene", password_hash="x", role="dept_head")
        raed = User(email="permission-raed@example.test", name="Raed", password_hash="x", role="dept_head")
        node_type = OrgNodeType(code="PERMISSION_BRANCH", name_ar="Permission branch", sort_order=1)
        db.session.add_all((requester, irene, raed, node_type))
        db.session.flush()

        nodes = [
            OrgNode(type_id=node_type.id, name_ar=name, is_active=True)
            for name in ("Permission branch one", "Permission branch two", "Permission branch three")
        ]
        db.session.add_all(nodes)
        db.session.flush()
        requester.org_node_id = nodes[0].id
        db.session.add_all([
            OrgNodeAssignment(user_id=requester.id, node_id=nodes[0].id, is_primary=True),
            OrgNodeAssignment(user_id=requester.id, node_id=nodes[1].id, is_primary=False),
            OrgNodeAssignment(user_id=requester.id, node_id=nodes[2].id, is_primary=False),
            OrgNodeManager(node_id=nodes[0].id, manager_user_id=self.manager.id),
            OrgNodeManager(node_id=nodes[1].id, manager_user_id=irene.id),
            OrgNodeManager(node_id=nodes[2].id, manager_user_id=raed.id),
        ])
        db.session.add_all([
            UserPermission(user_id=requester.id, key=key, is_allowed=True)
            for key in ("PORTAL_READ", "HR_READ", "HR_REQUESTS_READ", "HR_REQUESTS_CREATE")
        ])
        db.session.commit()

        row = self._permission(requester)
        steps = start_request_flow(KIND_PERMISSION, row)
        db.session.commit()

        expected_ids = [self.manager.id, irene.id, raed.id]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].approver_user_id, self.manager.id)
        self.assertEqual(json.loads(steps[0].approver_user_ids), expected_ids)
        self.assertTrue(all(can_user_act(user, steps[0]) for user in (self.manager, irene, raed)))
        notified_ids = {
            notification.user_id
            for notification in Notification.query.filter_by(
                type="HR_APPROVAL",
                link_url=f"/portal/hr/approvals/permissions/{row.id}",
            ).all()
        }
        self.assertEqual(notified_ids, set(expected_ids))

        self.assertEqual(decide_request(KIND_PERMISSION, row, raed, "APPROVE"), "APPROVED")
        self.assertEqual(row.status, "APPROVED")
        self.assertEqual(steps[0].decided_by_id, raed.id)

        allowed_notification_ids = set(expected_ids) | {requester.id, self.hr.id}
        recipient_ids = {
            notification.user_id
            for notification in Notification.query.filter_by(
                link_url=f"/portal/hr/approvals/permissions/{row.id}",
            ).all()
        }
        self.assertEqual(recipient_ids, allowed_notification_ids)
        self.assertNotIn(self.general_director.id, recipient_ids)
        self.assertNotIn(self.secretary.id, recipient_ids)

        observer_ids = {
            observer.user_id
            for observer in HRRequestObserver.query.filter_by(
                request_kind=KIND_PERMISSION,
                request_id=row.id,
            ).all()
        }
        self.assertEqual(observer_ids, set(expected_ids) | {self.hr.id})

        client = self.app.test_client()
        self._login(client, requester.id)
        my_permissions = client.get("/portal/hr/me/permissions")
        self.assertEqual(my_permissions.status_code, 200)
        for name in ("Manager", "Irene", "Raed"):
            self.assertIn(name.encode("utf-8"), my_permissions.data)

    def test_employee_can_edit_a_submitted_permission(self):
        requester = User(
            email="editable-permission-requester@example.test",
            name="Editable Permission Requester",
            password_hash="x",
            role="employee",
        )
        db.session.add(requester)
        db.session.flush()
        db.session.add(EmployeeFile(
            user_id=requester.id,
            direct_manager_user_id=self.manager.id,
        ))
        db.session.add_all([
            UserPermission(user_id=requester.id, key=key, is_allowed=True)
            for key in ("PORTAL_READ", "HR_READ", "HR_REQUESTS_READ", "HR_REQUESTS_CREATE")
        ])
        row = self._permission(requester)
        start_request_flow(KIND_PERMISSION, row)
        db.session.commit()

        client = self.app.test_client()
        self._login(client, requester.id)
        listing = client.get("/portal/hr/me/permissions")
        self.assertEqual(listing.status_code, 200)
        self.assertIn(f"/portal/hr/permissions/{row.id}/edit".encode("utf-8"), listing.data)

        edit_page = client.get(f"/portal/hr/permissions/{row.id}/edit")
        self.assertEqual(edit_page.status_code, 200)
        response = client.post(
            f"/portal/hr/permissions/{row.id}/edit",
            data={
                "permission_type_id": self.permission_type.id,
                "day": "2026-09-02",
                "from_time": "11:00",
                "to_time": "12:30",
                "note": "Updated departure",
            },
        )
        self.assertEqual(response.status_code, 302)
        updated = db.session.get(HRPermissionRequest, row.id)
        self.assertEqual(updated.day, "2026-09-02")
        self.assertEqual(updated.from_time, "11:00")
        self.assertEqual(updated.to_time, "12:30")
        self.assertEqual(updated.note, "Updated departure")
        self.assertEqual(updated.status, "SUBMITTED")

    def test_employee_can_edit_unapproved_external_leave_and_its_details_are_visible(self):
        requester = User(
            email="editable-leave-requester@example.test",
            name="Editable Leave Requester",
            password_hash="x",
            role="employee",
        )
        db.session.add(requester)
        db.session.flush()
        db.session.add(EmployeeFile(
            user_id=requester.id,
            direct_manager_user_id=self.manager.id,
        ))
        db.session.add_all([
            UserPermission(user_id=requester.id, key=key, is_allowed=True)
            for key in ("PORTAL_READ", "HR_READ", "HR_REQUESTS_READ", "HR_REQUESTS_CREATE")
        ])
        row = HRLeaveRequest(
            user_id=requester.id,
            leave_type_id=self.normal_type.id,
            start_date="2026-09-01",
            end_date="2026-09-02",
            days=2,
            leave_place="EXTERNAL",
            travel_country="Before update",
            status="SUBMITTED",
            submitted_at=datetime.utcnow(),
        )
        db.session.add(row)
        db.session.flush()
        start_request_flow(KIND_LEAVE, row)
        db.session.commit()

        client = self.app.test_client()
        self._login(client, requester.id)
        listing = client.get("/portal/hr/me/leaves")
        self.assertEqual(listing.status_code, 200)
        self.assertIn(f"/portal/hr/me/leaves/{row.id}/edit".encode("utf-8"), listing.data)

        edit_page = client.get(f"/portal/hr/me/leaves/{row.id}/edit")
        self.assertEqual(edit_page.status_code, 200)
        self.assertIn(b"Before update", edit_page.data)
        response = client.post(
            f"/portal/hr/me/leaves/{row.id}/edit",
            data={
                "leave_type_id": self.normal_type.id,
                "start_date": "2026-09-03",
                "end_date": "2026-09-05",
                "days": "3",
                "leave_place": "EXTERNAL",
                "travel_country": "Jordan",
                "travel_city": "Amman",
                "travel_address": "External address",
                "travel_contact_phone": "12345",
                "travel_purpose": "Family visit",
                "border_crossing": "Bridge",
                "note": "Updated external leave",
            },
        )
        self.assertEqual(response.status_code, 302)
        updated = db.session.get(HRLeaveRequest, row.id)
        self.assertEqual(updated.status, "SUBMITTED")
        self.assertEqual(updated.start_date, "2026-09-03")
        self.assertEqual(updated.end_date, "2026-09-05")
        self.assertEqual(updated.leave_place, "EXTERNAL")
        self.assertEqual(updated.travel_country, "Jordan")
        self.assertEqual(updated.travel_city, "Amman")
        self.assertEqual(updated.travel_purpose, "Family visit")

        self._login(client, self.manager.id)
        detail = client.get(f"/portal/hr/approvals/leaves/{row.id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"Jordan", detail.data)
        self.assertIn(b"Family visit", detail.data)

    def test_general_director_board_scope_contains_directorate_employee(self):
        visible_ids = board_visible_user_ids(self.general_director)
        self.assertIsNotNone(visible_ids)
        self.assertIn(self.employee.id, visible_ids)

    def test_absence_board_and_excel_export_show_only_approved_rows(self):
        row = self._leave(self.normal_type)
        start_request_flow(KIND_LEAVE, row)
        decide_request(KIND_LEAVE, row, self.manager, "APPROVE")
        db.session.commit()

        client = self.app.test_client()
        self._login(client, self.general_director.id)
        dashboard = client.get("/portal/hr/absence-board?day=2026-09-01")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("لوحة الموظفين المجازين والمغادرين".encode("utf-8"), dashboard.data)
        self.assertIn(b"view=permissions", dashboard.data)

        departures = client.get("/portal/hr/absence-board?day=2026-09-01&view=permissions")
        self.assertEqual(departures.status_code, 200)
        self.assertIn("الموظفون المغادرون".encode("utf-8"), departures.data)

        page = client.get("/portal/hr/absence-board/leaves?day=2026-09-01")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Employee", page.data)

        export = client.get("/portal/hr/absence-board/leaves?day=2026-09-01&export=xlsx")
        self.assertEqual(export.status_code, 200)
        self.assertEqual(export.mimetype, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    def test_assigned_manager_can_use_inbox_without_a_global_approve_permission(self):
        row = self._leave(self.normal_type)
        start_request_flow(KIND_LEAVE, row)
        db.session.commit()

        client = self.app.test_client()
        self._login(client, self.manager.id)
        inbox = client.get("/portal/hr/approvals")
        self.assertEqual(inbox.status_code, 200)
        detail = client.get(f"/portal/hr/approvals/leaves/{row.id}")
        self.assertEqual(detail.status_code, 200)
        response = client.post(
            f"/portal/hr/approvals/leaves/{row.id}",
            data={"action": "APPROVE", "decision_note": "approved"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.session.get(HRLeaveRequest, row.id).status, "APPROVED")
        history = client.get("/portal/hr/approvals?status=APPROVED")
        self.assertEqual(history.status_code, 200)
        self.assertIn(b"Employee", history.data)


if __name__ == "__main__":
    unittest.main()
