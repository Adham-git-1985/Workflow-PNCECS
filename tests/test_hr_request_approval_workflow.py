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
    Delegation,
    Directorate,
    EmployeeFile,
    EmployeeSecondment,
    HRLeaveRequest,
    HRLeaveType,
    HRPermissionRequest,
    HRPermissionRequestRevision,
    HRPermissionType,
    HRRequestObserver,
    Notification,
    NotificationEmailDelivery,
    Organization,
    OrgNode,
    OrgNodeAssignment,
    OrgNodeManager,
    OrgNodeType,
    OrgUnitManager,
    RolePermission,
    SystemSetting,
    User,
    UserPermission,
)
from portal import portal_bp
from services.hr_request_workflow import (
    ESCALATION_TARGET_HR,
    ESCALATION_TARGET_NONE,
    ESCALATION_TARGET_SECRETARY_GENERAL,
    ESCALATION_UNIT_HOURS,
    ESCALATION_UNIT_MINUTES,
    KIND_LEAVE,
    KIND_PERMISSION,
    board_visible_user_ids,
    can_user_act,
    current_step,
    decide_request,
    escalation_setting_key,
    _notify,
    process_pending_approvals,
    request_ids_user_can_act_on,
    start_request_flow,
)
from services.notification_email import _can_receive_hr_request_notification_email


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
        for key in (
            "_login_user",
            "_role_permission_keys",
            "_portal_approvable_request_ids",
        ):
            g.pop(key, None)
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

    def _configure_two_stage_escalation(
        self,
        request_kind,
        first_user_id,
        second_user_id,
    ):
        policies = {
            1: {
                "value": "30",
                "unit": ESCALATION_UNIT_MINUTES,
                "target": f"USER:{first_user_id}",
            },
            2: {
                "value": "2",
                "unit": ESCALATION_UNIT_HOURS,
                "target": f"USER:{second_user_id}",
            },
        }
        db.session.add_all([
            SystemSetting(
                key=escalation_setting_key(request_kind, level, field),
                value=value,
            )
            for level, policy in policies.items()
            for field, value in policy.items()
        ])
        db.session.flush()

    def _assert_two_stage_escalation(self, request_kind, row):
        first_approver = User(
            email=f"{request_kind.lower()}-first-escalation@example.test",
            name=f"{request_kind} First Escalation",
            password_hash="x",
            role="employee",
        )
        second_approver = User(
            email=f"{request_kind.lower()}-second-escalation@example.test",
            name=f"{request_kind} Second Escalation",
            password_hash="x",
            role="employee",
        )
        db.session.add_all((first_approver, second_approver))
        db.session.flush()
        self._configure_two_stage_escalation(
            request_kind,
            first_approver.id,
            second_approver.id,
        )

        assigned_at = datetime(2026, 9, 8, 8, 0)
        start_request_flow(request_kind, row, now=assigned_at)
        step = current_step(request_kind, row.id)
        first_due_at = assigned_at + timedelta(minutes=30)
        self.assertEqual(step.due_at, first_due_at)

        first_result = process_pending_approvals(
            now=first_due_at,
            send_notifications=True,
        )
        self.assertEqual(first_result["escalated"], 1)
        self.assertEqual(step.approver_user_id, first_approver.id)
        self.assertEqual(step.escalation_count, 1)
        self.assertEqual(step.escalated_from_user_id, self.manager.id)
        self.assertEqual(step.due_at, first_due_at + timedelta(hours=2))
        self.assertTrue(can_user_act(first_approver, step))
        self.assertFalse(can_user_act(self.manager, step))

        second_due_at = step.due_at
        second_result = process_pending_approvals(
            now=second_due_at,
            send_notifications=True,
        )
        self.assertEqual(second_result["escalated"], 1)
        self.assertEqual(step.approver_user_id, second_approver.id)
        self.assertEqual(step.escalation_count, 2)
        self.assertEqual(step.escalated_from_user_id, first_approver.id)
        self.assertIsNone(step.due_at)
        self.assertTrue(can_user_act(second_approver, step))
        self.assertFalse(can_user_act(first_approver, step))

        final_result = process_pending_approvals(
            now=second_due_at + timedelta(days=3),
            send_notifications=True,
        )
        self.assertEqual(final_result["escalated"], 0)
        self.assertEqual(step.approver_user_id, second_approver.id)
        escalation_recipient_ids = {
            notification.user_id
            for notification in Notification.query.filter_by(
                type="HR_APPROVAL_ESCALATED",
            ).all()
        }
        self.assertEqual(
            escalation_recipient_ids,
            {first_approver.id, second_approver.id},
        )

    def test_normal_leave_is_final_after_direct_manager_without_observer_cc(self):
        row = self._leave(self.normal_type)
        steps = start_request_flow(KIND_LEAVE, row)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].approver_user_id, self.manager.id)

        result = decide_request(KIND_LEAVE, row, self.manager, "APPROVE", "ok")
        db.session.commit()

        self.assertEqual(result, "APPROVED")
        self.assertEqual(row.status, "APPROVED")
        observer_ids = {observer.user_id for observer in HRRequestObserver.query.filter_by(request_kind=KIND_LEAVE, request_id=row.id).all()}
        self.assertFalse(observer_ids)
        cc_ids = {
            notification.user_id
            for notification in Notification.query.filter_by(
                type="HR_REQUEST_CC",
                link_url=f"/portal/hr/approvals/leaves/{row.id}",
            ).all()
        }
        self.assertFalse(cc_ids)

    def test_hr_management_permission_does_not_subscribe_to_all_leave_updates(self):
        db.session.add(UserPermission(
            user_id=self.general_director.id,
            key="HR_EMPLOYEE_MANAGE",
            is_allowed=True,
        ))
        row = self._leave(self.normal_type)
        start_request_flow(KIND_LEAVE, row)
        decide_request(KIND_LEAVE, row, self.manager, "APPROVE")
        db.session.commit()

        cc_ids = {
            notification.user_id
            for notification in Notification.query.filter_by(
                type="HR_REQUEST_CC",
                link_url=f"/portal/hr/approvals/leaves/{row.id}",
            ).all()
        }
        self.assertFalse(cc_ids)

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
        outsider = User(email="leave-observer@example.test", name="Leave Observer", password_hash="x", role="employee")
        db.session.add(outsider)
        db.session.flush()
        db.session.add(UserPermission(
            user_id=outsider.id,
            key="NOTIFICATIONS_GLOBAL_OBSERVER",
            is_allowed=True,
        ))
        db.session.commit()

        row = self._leave(self.external_type)
        steps = start_request_flow(KIND_LEAVE, row)
        self.assertEqual([step.stage_code for step in steps], ["DIRECT_MANAGER", "HR", "SECRETARY_GENERAL"])
        self.assertEqual(json.loads(steps[1].approver_user_ids), [self.hr.id])
        self.assertEqual(json.loads(steps[2].approver_user_ids), [self.secretary.id])

        self.assertEqual(decide_request(KIND_LEAVE, row, self.manager, "APPROVE"), "NEXT")
        self.assertEqual(row.status, "SUBMITTED")
        self.assertEqual(current_step(KIND_LEAVE, row.id).stage_code, "HR")
        self.assertTrue(can_user_act(self.hr, current_step(KIND_LEAVE, row.id)))
        self.assertFalse(can_user_act(self.secretary, current_step(KIND_LEAVE, row.id)))

        self.assertEqual(decide_request(KIND_LEAVE, row, self.hr, "APPROVE"), "NEXT")
        self.assertEqual(current_step(KIND_LEAVE, row.id).stage_code, "SECRETARY_GENERAL")
        self.assertEqual(decide_request(KIND_LEAVE, row, self.secretary, "APPROVE"), "APPROVED")
        self.assertEqual(row.status, "APPROVED")
        db.session.commit()

        recipient_ids = {
            notification.user_id
            for notification in Notification.query.filter_by(
                link_url=f"/portal/hr/approvals/leaves/{row.id}",
                is_mirror=False,
            ).all()
        }
        self.assertEqual(
            recipient_ids,
            {self.employee.id, self.manager.id, self.hr.id, self.secretary.id},
        )
        self.assertNotIn(outsider.id, recipient_ids)
        self.assertFalse(can_user_act(outsider, current_step(KIND_LEAVE, row.id)))
        self.assertEqual(Notification.query.filter_by(
            type="HR_REQUEST_CC",
            link_url=f"/portal/hr/approvals/leaves/{row.id}",
        ).count(), 0)

    def test_minister_leave_routes_directly_to_secretary_general(self):
        self.employee.role = "MINISTER"
        db.session.commit()

        row = self._leave(self.normal_type)
        steps = start_request_flow(KIND_LEAVE, row)

        self.assertEqual(
            [step.stage_code for step in steps],
            ["SECRETARY_GENERAL"],
        )
        self.assertEqual(steps[0].approver_user_id, self.secretary.id)
        self.assertTrue(can_user_act(self.secretary, current_step(KIND_LEAVE, row.id)))

    def test_leave_requester_can_download_official_pdf_and_word_forms(self):
        row = self._leave(self.normal_type)
        db.session.add(UserPermission(
            user_id=self.employee.id,
            key="PORTAL_READ",
            is_allowed=True,
        ))
        db.session.commit()
        client = self.app.test_client()
        self._login(client, self.employee.id)

        pdf_response = client.get(f"/portal/hr/me/leaves/{row.id}/form.pdf")
        word_response = client.get(f"/portal/hr/me/leaves/{row.id}/form.docx")

        self.assertEqual(pdf_response.status_code, 200)
        self.assertEqual(pdf_response.mimetype, "application/pdf")
        self.assertEqual(word_response.status_code, 200)
        self.assertIn("wordprocessingml", word_response.mimetype)
        employee_name = self.employee.full_name or self.employee.name or self.employee.email
        self.assertIn(f"{employee_name} - إجازة.pdf", unquote(pdf_response.headers["Content-Disposition"]))
        self.assertIn(f"{employee_name} - إجازة.docx", unquote(word_response.headers["Content-Disposition"]))

    def test_permission_official_forms_enforce_request_visibility(self):
        row = self._permission()
        for user in (self.employee, self.manager):
            if not UserPermission.query.filter_by(user_id=user.id, key="PORTAL_READ").first():
                db.session.add(UserPermission(user_id=user.id, key="PORTAL_READ", is_allowed=True))
        db.session.commit()
        client = self.app.test_client()
        self._login(client, self.employee.id)
        for extension in ("pdf", "docx"):
            response = client.get(f"/portal/hr/me/permissions/{row.id}/form.{extension}")
            self.assertEqual(response.status_code, 200)
            self.assertIn("no-store", response.headers["Cache-Control"])
            employee_name = self.employee.full_name or self.employee.name or self.employee.email
            self.assertIn(
                f"{employee_name} - مغادرة.{extension}",
                unquote(response.headers["Content-Disposition"]),
            )
        self.assertEqual(client.get(f"/portal/hr/me/permissions/{row.id}/form.exe").status_code, 404)
        # No workflow assignment grants this manager visibility yet.
        self._login(client, self.manager.id)
        with patch("portal.routes.can_view_hr_request", return_value=False):
            for extension in ("pdf", "docx"):
                self.assertEqual(client.get(f"/portal/hr/me/permissions/{row.id}/form.{extension}").status_code, 403)

    def test_pending_approval_form_does_not_print_candidate_names_as_signatories(self):
        from portal.routes import _leave_form_step_name
        row = self._leave(self.normal_type)
        start_request_flow(KIND_LEAVE, row)
        step = current_step(KIND_LEAVE, row.id)
        self.assertEqual(_leave_form_step_name(step), "")
        decide_request(KIND_LEAVE, row, self.manager, "APPROVE")
        db.session.commit()
        self.assertEqual(_leave_form_step_name(step), self.manager.full_name or self.manager.name)

    def test_view_all_and_approve_grant_global_leave_and_permission_approval(self):
        db.session.add_all([
            RolePermission(role="GENERAL-SECRETARY", permission=permission)
            for permission in (
                "PORTAL_READ",
                "HR_READ",
                "HR_REQUESTS_APPROVE",
                "HR_REQUESTS_VIEW_ALL",
            )
        ])
        limited_approver = User(
            email="limited-approver@example.test",
            name="Limited Approver",
            password_hash="x",
            role="LIMITED_APPROVER",
        )
        db.session.add(limited_approver)
        db.session.flush()
        db.session.add(RolePermission(
            role="LIMITED_APPROVER",
            permission="HR_REQUESTS_APPROVE",
        ))

        leave = self._leave(self.normal_type)
        permission_request = self._permission()
        leave_step = start_request_flow(KIND_LEAVE, leave)[0]
        permission_step = start_request_flow(KIND_PERMISSION, permission_request)[0]
        db.session.commit()

        self.assertTrue(can_user_act(self.secretary, leave_step))
        self.assertTrue(can_user_act(self.secretary, permission_step))
        self.assertFalse(can_user_act(limited_approver, leave_step))
        self.assertEqual(
            request_ids_user_can_act_on(self.secretary, KIND_LEAVE),
            [leave.id],
        )
        self.assertEqual(
            request_ids_user_can_act_on(self.secretary, KIND_PERMISSION),
            [permission_request.id],
        )

        client = self.app.test_client()
        self._login(client, self.secretary.id)
        with patch("portal.routes.render_template", return_value="ok") as render:
            detail = client.get(
                f"/portal/hr/approvals/permissions/{permission_request.id}"
            )
        self.assertEqual(detail.status_code, 200)
        self.assertTrue(render.call_args.kwargs["can_act"])

        response = client.post(
            f"/portal/hr/approvals/permissions/{permission_request.id}",
            data={"action": "APPROVE"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(permission_request.status, "APPROVED")

    def test_submission_notifies_only_requester_and_assigned_manager(self):
        db.session.add_all([
            RolePermission(role="GENERAL-SECRETARY", permission=permission)
            for permission in ("HR_REQUESTS_APPROVE", "HR_REQUESTS_VIEW_ALL")
        ])
        limited_approver = User(
            email="submission-limited-approver@example.test",
            name="Submission Limited Approver",
            password_hash="x",
            role="LIMITED_APPROVER",
        )
        db.session.add(limited_approver)
        db.session.flush()
        db.session.add(RolePermission(
            role="LIMITED_APPROVER",
            permission="HR_REQUESTS_APPROVE",
        ))

        leave = self._leave(self.normal_type)
        permission_request = self._permission()
        start_request_flow(KIND_LEAVE, leave)
        start_request_flow(KIND_PERMISSION, permission_request)
        db.session.commit()

        expected_by_link = {
            f"/portal/hr/approvals/leaves/{leave.id}": {
                self.employee.id,
                self.manager.id,
            },
            f"/portal/hr/approvals/permissions/{permission_request.id}": {
                self.employee.id,
                self.manager.id,
            },
        }
        for link_url, expected_ids in expected_by_link.items():
            notifications = Notification.query.filter_by(link_url=link_url).all()
            self.assertEqual({notification.user_id for notification in notifications}, expected_ids)
            self.assertTrue(all(notification.source == "portal" for notification in notifications))
            self.assertTrue(all(not notification.is_read for notification in notifications))
            self.assertEqual(
                {notification.user_id for notification in notifications if notification.type == "HR_REQUEST_SUBMITTED"},
                {self.employee.id},
            )
            manager_notification = next(
                notification
                for notification in notifications
                if notification.user_id == self.manager.id
            )
            self.assertTrue(
                _can_receive_hr_request_notification_email(
                    self.manager,
                    manager_notification,
                )
            )
            self.assertFalse(
                _can_receive_hr_request_notification_email(
                    self.secretary,
                    manager_notification,
                )
            )
            self.assertFalse(
                _can_receive_hr_request_notification_email(
                    self.hr,
                    manager_notification,
                )
            )
            self.assertNotIn(limited_approver.id, {notification.user_id for notification in notifications})
            self.assertNotIn(self.secretary.id, {notification.user_id for notification in notifications})
            self.assertNotIn(self.hr.id, {notification.user_id for notification in notifications})
        self.assertEqual(NotificationEmailDelivery.query.count(), 0)

    def test_secretary_general_returns_to_the_leave_after_final_approval(self):
        db.session.add(UserPermission(
            user_id=self.secretary.id,
            key="PORTAL_READ",
            is_allowed=True,
        ))
        row = self._leave(self.external_type)
        start_request_flow(KIND_LEAVE, row)
        decide_request(KIND_LEAVE, row, self.manager, "APPROVE")
        decide_request(KIND_LEAVE, row, self.hr, "APPROVE")
        db.session.commit()

        client = self.app.test_client()
        self._login(client, self.secretary.id)
        response = client.post(
            f"/portal/hr/approvals/leaves/{row.id}",
            data={"action": "APPROVE"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"],
            f"/portal/hr/approvals/leaves/{row.id}",
        )
        self.assertEqual(
            client.get(response.headers["Location"]).status_code,
            200,
        )

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
        self.assertEqual(step.escalation_reason, "SLA_1_GENERAL_DIRECTOR")

    def test_overdue_permission_step_is_not_escalated(self):
        row = self._permission()
        start_request_flow(KIND_PERMISSION, row)
        step = current_step(KIND_PERMISSION, row.id)
        step.due_at = datetime.utcnow() - timedelta(minutes=1)
        db.session.flush()

        result = process_pending_approvals(now=datetime.utcnow(), send_notifications=False)

        self.assertEqual(result["escalated"], 0)
        self.assertEqual(row.status, "SUBMITTED")
        self.assertEqual(step.status, "PENDING")
        self.assertEqual(step.approver_user_id, self.manager.id)
        self.assertIsNone(step.escalated_at)
        self.assertIsNone(step.escalation_reason)

    def test_leave_uses_two_configured_escalation_periods_and_targets(self):
        row = self._leave(self.normal_type)
        self._assert_two_stage_escalation(KIND_LEAVE, row)

    def test_permission_uses_two_configured_escalation_periods_and_targets(self):
        row = self._permission()
        self._assert_two_stage_escalation(KIND_PERMISSION, row)

    def test_permission_escalation_resolves_hr_then_secretary_general(self):
        configured_values = {
            escalation_setting_key(KIND_PERMISSION, 1, "VALUE"): "10",
            escalation_setting_key(KIND_PERMISSION, 1, "UNIT"): ESCALATION_UNIT_MINUTES,
            escalation_setting_key(KIND_PERMISSION, 1, "TARGET"): ESCALATION_TARGET_HR,
            escalation_setting_key(KIND_PERMISSION, 2, "VALUE"): "1",
            escalation_setting_key(KIND_PERMISSION, 2, "UNIT"): ESCALATION_UNIT_HOURS,
            escalation_setting_key(KIND_PERMISSION, 2, "TARGET"): ESCALATION_TARGET_SECRETARY_GENERAL,
        }
        db.session.add_all([
            SystemSetting(key=key, value=value)
            for key, value in configured_values.items()
        ])
        row = self._permission()
        assigned_at = datetime(2026, 9, 8, 8, 0)
        start_request_flow(KIND_PERMISSION, row, now=assigned_at)
        step = current_step(KIND_PERMISSION, row.id)

        first_due_at = assigned_at + timedelta(minutes=10)
        self.assertEqual(step.due_at, first_due_at)
        process_pending_approvals(now=first_due_at, send_notifications=True)
        self.assertEqual(step.approver_user_id, self.hr.id)

        second_due_at = first_due_at + timedelta(hours=1)
        self.assertEqual(step.due_at, second_due_at)
        process_pending_approvals(now=second_due_at, send_notifications=True)
        self.assertEqual(step.approver_user_id, self.secretary.id)
        self.assertEqual(step.escalation_count, 2)
        self.assertIsNone(step.due_at)

    def test_escalation_settings_page_saves_all_four_policies(self):
        db.session.add(UserPermission(
            user_id=self.secretary.id,
            key="HR_REQUESTS_VIEW_ALL",
            is_allowed=True,
        ))
        row = self._leave(self.normal_type)
        permission_row = self._permission()
        assigned_at = datetime(2026, 9, 8, 9, 0)
        start_request_flow(KIND_LEAVE, row, now=assigned_at)
        start_request_flow(KIND_PERMISSION, permission_row, now=assigned_at)
        db.session.commit()

        client = self.app.test_client()
        self._login(client, self.secretary.id)
        response = client.post(
            "/portal/hr/alerts/escalation-settings",
            data={
                "leave_escalation_1_value": "15",
                "leave_escalation_1_unit": ESCALATION_UNIT_MINUTES,
                "leave_escalation_1_target": f"USER:{self.general_director.id}",
                "leave_escalation_2_value": "2",
                "leave_escalation_2_unit": ESCALATION_UNIT_HOURS,
                "leave_escalation_2_target": f"USER:{self.secretary.id}",
                "permission_escalation_1_value": "45",
                "permission_escalation_1_unit": ESCALATION_UNIT_MINUTES,
                "permission_escalation_1_target": f"USER:{self.hr.id}",
                "permission_escalation_2_value": "3",
                "permission_escalation_2_unit": ESCALATION_UNIT_HOURS,
                "permission_escalation_2_target": ESCALATION_TARGET_NONE,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/portal/hr/alerts")
        expected_values = {
            escalation_setting_key(KIND_LEAVE, 1, "VALUE"): "15",
            escalation_setting_key(KIND_LEAVE, 1, "UNIT"): ESCALATION_UNIT_MINUTES,
            escalation_setting_key(KIND_LEAVE, 1, "TARGET"): f"USER:{self.general_director.id}",
            escalation_setting_key(KIND_LEAVE, 2, "VALUE"): "2",
            escalation_setting_key(KIND_LEAVE, 2, "UNIT"): ESCALATION_UNIT_HOURS,
            escalation_setting_key(KIND_LEAVE, 2, "TARGET"): f"USER:{self.secretary.id}",
            escalation_setting_key(KIND_PERMISSION, 1, "VALUE"): "45",
            escalation_setting_key(KIND_PERMISSION, 1, "UNIT"): ESCALATION_UNIT_MINUTES,
            escalation_setting_key(KIND_PERMISSION, 1, "TARGET"): f"USER:{self.hr.id}",
            escalation_setting_key(KIND_PERMISSION, 2, "VALUE"): "3",
            escalation_setting_key(KIND_PERMISSION, 2, "UNIT"): ESCALATION_UNIT_HOURS,
            escalation_setting_key(KIND_PERMISSION, 2, "TARGET"): ESCALATION_TARGET_NONE,
        }
        stored_values = {
            setting.key: setting.value
            for setting in SystemSetting.query.filter(
                SystemSetting.key.in_(expected_values),
            ).all()
        }
        self.assertEqual(stored_values, expected_values)
        step = current_step(KIND_LEAVE, row.id)
        self.assertEqual(step.due_at, assigned_at + timedelta(minutes=15))
        permission_step = current_step(KIND_PERMISSION, permission_row.id)
        self.assertEqual(
            permission_step.due_at,
            assigned_at + timedelta(minutes=45),
        )

        with (
            patch(
                "portal.routes._check_pending_leave_requests",
                return_value={"pending": [], "pending_days": 1},
            ),
            patch("portal.routes.render_template", return_value="ok") as render,
        ):
            page = client.get("/portal/hr/alerts")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(render.call_args.args[0], "portal/hr/alerts.html")
        self.assertTrue(render.call_args.kwargs["can_manage_escalation"])
        self.assertEqual(
            render.call_args.kwargs["escalation_policies"][KIND_LEAVE][1]["value"],
            15,
        )
        self.assertEqual(
            render.call_args.kwargs["escalation_policies"][KIND_PERMISSION][2]["target"],
            ESCALATION_TARGET_NONE,
        )

    def test_escalation_settings_reject_second_level_when_first_is_disabled(self):
        db.session.add(UserPermission(
            user_id=self.secretary.id,
            key="HR_REQUESTS_VIEW_ALL",
            is_allowed=True,
        ))
        db.session.commit()

        client = self.app.test_client()
        self._login(client, self.secretary.id)
        response = client.post(
            "/portal/hr/alerts/escalation-settings",
            data={
                "leave_escalation_1_value": "1",
                "leave_escalation_1_unit": ESCALATION_UNIT_HOURS,
                "leave_escalation_1_target": ESCALATION_TARGET_NONE,
                "leave_escalation_2_value": "2",
                "leave_escalation_2_unit": ESCALATION_UNIT_HOURS,
                "leave_escalation_2_target": f"USER:{self.secretary.id}",
                "permission_escalation_1_value": "30",
                "permission_escalation_1_unit": ESCALATION_UNIT_MINUTES,
                "permission_escalation_1_target": ESCALATION_TARGET_NONE,
                "permission_escalation_2_value": "30",
                "permission_escalation_2_unit": ESCALATION_UNIT_MINUTES,
                "permission_escalation_2_target": ESCALATION_TARGET_NONE,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/portal/hr/alerts")
        self.assertEqual(SystemSetting.query.count(), 0)

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

        allowed_notification_ids = set(expected_ids) | {requester.id}
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

    def test_employee_can_reopen_an_approved_permission_and_restart_approval(self):
        db.session.add_all([
            UserPermission(user_id=self.employee.id, key=key, is_allowed=True)
            for key in ("PORTAL_READ", "HR_READ", "HR_REQUESTS_READ", "HR_REQUESTS_CREATE")
        ])
        row = self._permission()
        first_round = start_request_flow(KIND_PERMISSION, row)
        self.assertEqual(decide_request(KIND_PERMISSION, row, self.manager, "APPROVE"), "APPROVED")
        db.session.commit()

        client = self.app.test_client()
        self._login(client, self.employee.id)
        response = client.post(
            f"/portal/hr/permissions/{row.id}/edit",
            data={
                "permission_type_id": self.permission_type.id,
                "day": "2026-01-01",
                "from_time": "10:00",
                "to_time": "12:30",
                "note": "Returned later than planned",
                "reopen_reason": "Returned at 12:30",
            },
        )
        self.assertEqual(response.status_code, 302)

        updated = db.session.get(HRPermissionRequest, row.id)
        self.assertEqual(updated.status, "SUBMITTED")
        self.assertEqual(updated.day, "2026-01-01")
        self.assertEqual(updated.to_time, "12:30")

        revision = HRPermissionRequestRevision.query.filter_by(request_id=row.id).one()
        self.assertEqual(revision.revision_no, 1)
        self.assertEqual(revision.previous_day, "2026-09-01")
        self.assertEqual(revision.current_day, "2026-01-01")
        self.assertEqual(revision.previous_to_time, "11:00")
        self.assertEqual(revision.current_to_time, "12:30")
        self.assertEqual(revision.reason, "Returned at 12:30")

        rounds = {
            step.flow_revision
            for step in db.session.query(type(first_round[0])).filter_by(
                request_kind=KIND_PERMISSION,
                request_id=row.id,
            ).all()
        }
        self.assertEqual(rounds, {1, 2})
        current = current_step(KIND_PERMISSION, row.id)
        self.assertIsNotNone(current)
        self.assertEqual(current.flow_revision, 2)
        self.assertEqual(current.status, "PENDING")

        notified_ids = {
            notification.user_id
            for notification in Notification.query.filter_by(type="HR_PERMISSION_REOPENED").all()
        }
        self.assertEqual(notified_ids, {self.employee.id, self.manager.id})

        self._login(client, self.manager.id)
        detail = client.get(f"/portal/hr/approvals/permissions/{row.id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("12:30", detail.get_data(as_text=True))

    def test_new_permission_allows_a_selected_date_and_24_hour_time_choices(self):
        db.session.add_all([
            UserPermission(user_id=self.employee.id, key=key, is_allowed=True)
            for key in ("PORTAL_READ", "HR_READ", "HR_REQUESTS_READ", "HR_REQUESTS_CREATE")
        ])
        db.session.commit()
        client = self.app.test_client()
        self._login(client, self.employee.id)

        form = client.get("/portal/hr/me/permissions/new")
        self.assertEqual(form.status_code, 200)
        html = form.get_data(as_text=True)
        self.assertIn('name="from_time"', html)
        self.assertIn('value="08:00"', html)
        self.assertIn('value="12:30"', html)
        self.assertIn('value="15:00"', html)
        self.assertNotIn('value="07:55"', html)
        self.assertNotIn('type="time"', html)
        self.assertIn('type="date"', html)
        self.assertIn('دليل الإجازات والمغادرات', html)

        response = client.post(
            "/portal/hr/me/permissions/new",
            data={
                "permission_type_id": self.permission_type.id,
                "day": "2000-01-01",
                "from_time": "11:00",
                "to_time": "12:30",
                "note": "Selected-date permission",
            },
        )
        self.assertEqual(response.status_code, 302)
        created = HRPermissionRequest.query.order_by(HRPermissionRequest.id.desc()).first()
        self.assertEqual(created.day, "2000-01-01")
        self.assertEqual(created.from_time, "11:00")
        self.assertEqual(created.to_time, "12:30")

    def test_permission_rejects_times_outside_the_workday(self):
        db.session.add_all([
            UserPermission(user_id=self.employee.id, key=key, is_allowed=True)
            for key in ("PORTAL_READ", "HR_READ", "HR_REQUESTS_READ", "HR_REQUESTS_CREATE")
        ])
        db.session.commit()
        client = self.app.test_client()
        self._login(client, self.employee.id)

        response = client.post(
            "/portal/hr/me/permissions/new",
            data={
                "permission_type_id": self.permission_type.id,
                "day": date.today().isoformat(),
                "from_time": "07:55",
                "to_time": "12:30",
                "note": "Outside the permitted workday",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(HRPermissionRequest.query.count(), 0)
        self.assertIn("08:00", response.get_data(as_text=True))

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

    def test_requester_can_follow_leave_and_departure_approval_in_arabic(self):
        db.session.add_all([
            UserPermission(user_id=self.employee.id, key=key, is_allowed=True)
            for key in ("PORTAL_READ", "HR_REQUESTS_READ")
        ])
        leave = self._leave(self.normal_type)
        permission = self._permission()
        start_request_flow(KIND_LEAVE, leave)
        start_request_flow(KIND_PERMISSION, permission)
        db.session.commit()

        client = self.app.test_client()
        self._login(client, self.employee.id)

        leave_page = client.get(f"/portal/hr/approvals/leaves/{leave.id}")
        self.assertEqual(leave_page.status_code, 200)
        leave_body = leave_page.get_data(as_text=True)
        self.assertIn("متابعة طلب إجازة", leave_body)
        self.assertIn("قيد الاعتماد — بانتظار المسؤولون على الهيكلية", leave_body)
        self.assertIn("مسار الاعتماد", leave_body)
        self.assertNotIn("SUBMITTED", leave_body)

        permission_page = client.get(f"/portal/hr/approvals/permissions/{permission.id}")
        self.assertEqual(permission_page.status_code, 200)
        permission_body = permission_page.get_data(as_text=True)
        self.assertIn("متابعة طلب مغادرة", permission_body)
        self.assertIn("قيد الاعتماد — بانتظار المسؤولون على الهيكلية", permission_body)
        self.assertIn("مسار الاعتماد", permission_body)
        self.assertNotIn("SUBMITTED", permission_body)

        leave_list = client.get("/portal/hr/me/leaves")
        self.assertEqual(leave_list.status_code, 200)
        self.assertIn(f"/portal/hr/approvals/leaves/{leave.id}", leave_list.get_data(as_text=True))

        permission_list = client.get("/portal/hr/me/permissions")
        self.assertEqual(permission_list.status_code, 200)
        self.assertIn(f"/portal/hr/approvals/permissions/{permission.id}", permission_list.get_data(as_text=True))

    def test_unrelated_employee_cannot_view_or_receive_request_updates(self):
        outsider = User(
            email="outsider@example.test",
            name="Outsider",
            password_hash="x",
            role="employee",
        )
        db.session.add(outsider)
        db.session.flush()
        db.session.add_all([
            UserPermission(user_id=outsider.id, key=key, is_allowed=True)
            for key in ("PORTAL_READ", "HR_READ", "HR_REQUESTS_READ")
        ])
        row = self._leave(self.normal_type)
        start_request_flow(KIND_LEAVE, row)
        db.session.commit()

        client = self.app.test_client()
        self._login(client, outsider.id)
        self.assertEqual(client.get(f"/portal/hr/approvals/leaves/{row.id}").status_code, 403)
        self.assertEqual(client.get("/portal/hr/approvals").status_code, 403)

        _notify(
            [self.manager.id, outsider.id],
            "تحديث خاص بطلب إجازة.",
            kind=KIND_LEAVE,
            request_id=row.id,
        )
        db.session.commit()
        self.assertEqual(
            {
                notification.user_id
                for notification in Notification.query.filter_by(type="HR_APPROVAL").all()
            },
            {self.manager.id},
        )

    def test_legacy_unrelated_observer_cannot_receive_request_updates(self):
        outsider = User(
            email="legacy-observer@example.test",
            name="Legacy Observer",
            password_hash="x",
            role="employee",
        )
        db.session.add(outsider)
        db.session.flush()

        row = self._leave(self.normal_type)
        start_request_flow(KIND_LEAVE, row)
        db.session.add_all((
            HRRequestObserver(
                request_kind=KIND_LEAVE,
                request_id=row.id,
                user_id=self.hr.id,
                observer_scope="HR",
            ),
            HRRequestObserver(
                request_kind=KIND_LEAVE,
                request_id=row.id,
                user_id=outsider.id,
                observer_scope="HR",
            ),
        ))
        db.session.flush()

        _notify(
            [self.manager.id, self.hr.id, outsider.id],
            "تحديث خاص بطلب إجازة.",
            kind=KIND_LEAVE,
            request_id=row.id,
        )
        db.session.commit()

        self.assertEqual(
            {
                notification.user_id
                for notification in Notification.query.filter_by(type="HR_APPROVAL").all()
            },
            {self.manager.id},
        )

    def test_permission_updates_exclude_observers_and_global_approvers(self):
        db.session.add_all([
            RolePermission(role="GENERAL-SECRETARY", permission=permission)
            for permission in ("HR_REQUESTS_APPROVE", "HR_REQUESTS_VIEW_ALL")
        ])
        row = self._permission()
        start_request_flow(KIND_PERMISSION, row)
        db.session.add(HRRequestObserver(
            request_kind=KIND_PERMISSION,
            request_id=row.id,
            user_id=self.hr.id,
            observer_scope="HR",
        ))
        db.session.flush()

        _notify(
            [self.employee.id, self.manager.id, self.hr.id, self.secretary.id],
            "تحديث خاص بطلب مغادرة.",
            kind=KIND_PERMISSION,
            request_id=row.id,
            ntype="HR_PERMISSION_PRIVATE_UPDATE",
        )
        db.session.commit()

        recipient_ids = {
            notification.user_id
            for notification in Notification.query.filter_by(
                type="HR_PERMISSION_PRIVATE_UPDATE",
            ).all()
        }
        self.assertEqual(recipient_ids, {self.employee.id, self.manager.id})

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
            data={
                "action": "APPROVE",
                "decision_note": "approved",
                "covering_employee_name": "Covering Employee",
            },
        )
        self.assertEqual(response.status_code, 302)
        approved = db.session.get(HRLeaveRequest, row.id)
        self.assertEqual(approved.status, "APPROVED")
        self.assertEqual(approved.covering_employee_name, "Covering Employee")
        history = client.get("/portal/hr/approvals?status=APPROVED")
        self.assertEqual(history.status_code, 200)
        self.assertIn(b"Employee", history.data)


if __name__ == "__main__":
    unittest.main()
