import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask, g
from flask_login import LoginManager
from jinja2 import ChoiceLoader, DictLoader

from extensions import db
from models import (
    AuditLog,
    Department,
    Directorate,
    EmployeeFile,
    Message,
    MessageRecipient,
    Notification,
    Organization,
    OutboundMail,
    TroubleTicket,
    User,
)
from portal import portal_bp


class AdminBroadcastNotificationRouteTests(unittest.TestCase):
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
            SECRET_KEY="admin-broadcast-notifications-test",
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
        cls.app.jinja_loader = ChoiceLoader([
            DictLoader({
                "portal/layout.html": (
                    "<!doctype html>{% block content %}{% endblock %}"
                    "{% block scripts %}{% endblock %}"
                ),
            }),
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

        self.admin = self._user("admin@example.test", "Admin", role="ADMIN")
        self.super_admin = self._user("super@example.test", "Super Admin", role="SUPER_ADMIN")

        organization = Organization(name_ar="المنظمة", code="ORG")
        db.session.add(organization)
        db.session.flush()
        self.directorate_a = Directorate(
            organization_id=organization.id,
            name_ar="الإدارة أ",
            code="DIR-A",
        )
        self.directorate_b = Directorate(
            organization_id=organization.id,
            name_ar="الإدارة ب",
            code="DIR-B",
        )
        db.session.add_all((self.directorate_a, self.directorate_b))
        db.session.flush()
        self.department_a = Department(
            directorate_id=self.directorate_a.id,
            name_ar="الدائرة أ",
            code="DEP-A",
        )
        self.department_b = Department(
            directorate_id=self.directorate_b.id,
            name_ar="الدائرة ب",
            code="DEP-B",
        )
        db.session.add_all((self.department_a, self.department_b))
        db.session.flush()

        self.department_employee = self._user(
            "department@example.test",
            "Department Employee",
            department_id=self.department_a.id,
        )
        self.directorate_employee = self._user(
            "directorate@example.test",
            "Directorate Employee",
            directorate_id=self.directorate_a.id,
        )
        self.employee_file_member = self._user(
            "employee-file@example.test",
            "Employee File Member",
        )
        self.other_employee = self._user(
            "other@example.test",
            "Other Employee",
            department_id=self.department_b.id,
        )
        db.session.add(EmployeeFile(
            user_id=self.employee_file_member.id,
            department_id=self.department_a.id,
        ))
        self.linked_outbound = OutboundMail(
            ref_no="OUT-2026-01",
            category="GENERAL",
            recipient="الموظفون",
            subject="دليل استخدام الميزة الجديدة",
            body="شرح موجز للميزة الجديدة في النظام.",
            sent_date="2026-10-04",
            confidentiality="NORMAL",
            created_by_id=self.admin.id,
        )
        db.session.add(self.linked_outbound)
        db.session.commit()

    @staticmethod
    def _user(email, name, *, role="EMPLOYEE", department_id=None, directorate_id=None):
        user = User(
            email=email,
            name=name,
            password_hash="not-used-in-test",
            role=role,
            department_id=department_id,
            directorate_id=directorate_id,
        )
        db.session.add(user)
        db.session.flush()
        return user

    @staticmethod
    def _login(client, user_id):
        # This suite deliberately keeps one application context for its
        # in-memory database. Clear per-request delegation state before using
        # another test client so the prior actor cannot leak into this login.
        for key in (
            "delegation_checked",
            "delegations",
            "delegation",
            "effective_user",
            "_login_user",
            "acting_authorization_context",
            "execution_context",
        ):
            g.pop(key, None)
        with client.session_transaction() as session:
            session.clear()
            session["_user_id"] = str(user_id)
            session["_fresh"] = True

    @staticmethod
    def _sent_notifications():
        return Notification.query.filter_by(target_type="ADMIN_BROADCAST").all()

    def test_admin_can_open_composer_and_send_to_selected_employees(self):
        with self.app.test_client() as client:
            self._login(client, self.admin.id)
            form_response = client.get("/portal/admin/notifications/send")
            self.assertEqual(form_response.status_code, 200)
            self.assertIn("إرسال إشعار", form_response.get_data(as_text=True))

            response = client.post(
                "/portal/admin/notifications/send",
                data={
                    "message": "يرجى مراجعة النظام اليوم.",
                    "level": "URGENT",
                    "target_scope": "USERS",
                    "recipient_user_ids": [
                        str(self.department_employee.id),
                        str(self.other_employee.id),
                    ],
                },
            )
        self.assertEqual(response.status_code, 302)

        rows = self._sent_notifications()
        self.assertEqual({row.user_id for row in rows}, {
            self.department_employee.id,
            self.other_employee.id,
        })
        self.assertTrue(all(row.source == "portal" for row in rows))
        self.assertTrue(all(row.type == "URGENT" for row in rows))
        self.assertTrue(all(row.actor_id == self.admin.id for row in rows))
        self.assertEqual(len({row.event_key for row in rows}), 1)
        self.assertEqual(
            AuditLog.query.filter_by(action="PORTAL_ADMIN_NOTIFICATION_SEND").count(),
            1,
        )

        with self.app.test_client() as client:
            self._login(client, self.department_employee.id)
            denied = client.get("/portal/admin/notifications/send")
        self.assertEqual(denied.status_code, 403)

    def test_super_admin_can_send_to_an_entire_directorate(self):
        with self.app.test_client() as client:
            self._login(client, self.super_admin.id)
            response = client.post(
                "/portal/admin/notifications/send",
                data={
                    "message": "اجتماع للإدارة أ.",
                    "level": "INFO",
                    "target_scope": "DIRECTORATE",
                    "target_directorate_id": str(self.directorate_a.id),
                },
            )
        self.assertEqual(response.status_code, 302)

        self.assertEqual(
            {row.user_id for row in self._sent_notifications()},
            {
                self.department_employee.id,
                self.directorate_employee.id,
                self.employee_file_member.id,
            },
        )

    def test_admin_can_send_to_department_members_only(self):
        with self.app.test_client() as client:
            self._login(client, self.admin.id)
            response = client.post(
                "/portal/admin/notifications/send",
                data={
                    "message": "إشعار خاص بالدائرة أ.",
                    "level": "INFO",
                    "target_scope": "DEPARTMENT",
                    "target_department_id": str(self.department_a.id),
                },
            )
        self.assertEqual(response.status_code, 302)

        self.assertEqual(
            {row.user_id for row in self._sent_notifications()},
            {self.department_employee.id, self.employee_file_member.id},
        )

    def test_admin_can_link_a_one_way_notification_to_correspondence(self):
        with self.app.test_client() as client:
            self._login(client, self.admin.id)
            response = client.post(
                "/portal/admin/notifications/send",
                data={
                    "message": "تمت إضافة ميزة جديدة. راجع الدليل المرتبط.",
                    "level": "INFO",
                    "target_scope": "USERS",
                    "recipient_user_ids": [str(self.department_employee.id)],
                    "linked_correspondence": f"OUT:{self.linked_outbound.id}",
                },
            )
        self.assertEqual(response.status_code, 302)

        row = Notification.query.filter_by(
            target_type="ADMIN_BROADCAST_CORR_OUTBOUND",
            user_id=self.department_employee.id,
        ).one()
        self.assertEqual(row.target_id, self.linked_outbound.id)
        self.assertEqual(row.link_url, f"/portal/corr/outbound/{self.linked_outbound.id}")
        # A broadcast is a portal-system message, not an internal message
        # thread, so it cannot be replied to through the messages module.
        self.assertEqual(Message.query.count(), 0)
        self.assertEqual(MessageRecipient.query.count(), 0)

        with self.app.test_client() as client:
            self._login(client, self.department_employee.id)
            opened = client.get(f"/portal/notifications/{row.id}/open")
            self.assertEqual(opened.status_code, 302)
            self.assertEqual(
                opened.headers["Location"],
                f"{row.link_url}?feedback_notification={row.id}",
            )
            # The broadcast link did not grant correspondence-read access.
            denied = client.get(opened.headers["Location"])
        self.assertEqual(denied.status_code, 403)

    def test_recipient_can_submit_feedback_on_a_linked_update(self):
        # The notification does not create this access; it represents a
        # pre-existing assignment to the correspondence.
        self.linked_outbound.current_assignee_id = self.department_employee.id
        db.session.commit()

        with self.app.test_client() as client:
            self._login(client, self.admin.id)
            sent = client.post(
                "/portal/admin/notifications/send",
                data={
                    "message": "تحديث جديد في النظام. نرحب بملاحظاتكم.",
                    "level": "INFO",
                    "target_scope": "USERS",
                    "recipient_user_ids": [str(self.department_employee.id)],
                    "linked_correspondence": f"OUT:{self.linked_outbound.id}",
                },
            )
        self.assertEqual(sent.status_code, 302)
        notification = Notification.query.filter_by(
            target_type="ADMIN_BROADCAST_CORR_OUTBOUND",
            user_id=self.department_employee.id,
        ).one()

        with self.app.test_client() as client:
            self._login(client, self.department_employee.id)
            opened = client.get(f"/portal/notifications/{notification.id}/open")
            self.assertEqual(opened.status_code, 302)
            self.assertEqual(
                opened.headers["Location"],
                (
                    f"/portal/corr/outbound/{self.linked_outbound.id}"
                    f"?feedback_notification={notification.id}"
                ),
            )

            with patch("portal.routes.render_template", return_value="feedback view") as render:
                correspondence_view = client.get(opened.headers["Location"])
            self.assertEqual(correspondence_view.status_code, 200)
            self.assertEqual(render.call_args.args[0], "portal/corr/outbound_view.html")
            shown_feedback = render.call_args.kwargs["feedback_context"]
            self.assertEqual(shown_feedback["notification_id"], notification.id)
            self.assertEqual(
                shown_feedback["ticket_url"],
                f"/portal/trouble-tickets/feedback/{notification.id}",
            )

            form = client.get(f"/portal/trouble-tickets/feedback/{notification.id}")
            self.assertEqual(form.status_code, 200)
            self.assertIn("إرسال ملاحظات حول تحديث النظام", form.get_data(as_text=True))
            self.assertIn(self.linked_outbound.subject, form.get_data(as_text=True))
            self.assertEqual(TroubleTicket.query.count(), 0)

            submitted = client.post(
                f"/portal/trouble-tickets/feedback/{notification.id}",
                data={
                    "subject": "محاولة تغيير عنوان التذكرة",
                    "description": "أقترح شرحاً أوضح لإعدادات الخصوصية.",
                    "priority": "NORMAL",
                },
            )
        self.assertEqual(submitted.status_code, 302)

        ticket = TroubleTicket.query.one()
        self.assertEqual(ticket.requester_id, self.department_employee.id)
        self.assertEqual(ticket.category, "SYSTEM")
        self.assertIn("ملاحظات حول تحديث النظام", ticket.subject)
        self.assertNotIn("محاولة تغيير عنوان التذكرة", ticket.subject)
        self.assertIn("أقترح شرحاً أوضح لإعدادات الخصوصية.", ticket.description)
        self.assertIn(f"مرجع الإشعار: #{notification.id}", ticket.description)
        self.assertIn(f"/portal/corr/outbound/{self.linked_outbound.id}", ticket.description)
        self.assertIsNotNone(AuditLog.query.filter_by(
            action="TROUBLE_TICKET_FEEDBACK_CREATE",
            target_id=ticket.id,
        ).first())

        with self.app.test_client() as client:
            self._login(client, self.other_employee.id)
            forbidden_feedback = client.get(
                f"/portal/trouble-tickets/feedback/{notification.id}"
            )
        self.assertEqual(forbidden_feedback.status_code, 404)


if __name__ == "__main__":
    unittest.main()
