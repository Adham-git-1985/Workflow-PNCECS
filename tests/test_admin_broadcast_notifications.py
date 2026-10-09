import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask, g
from flask_login import LoginManager
from jinja2 import ChoiceLoader, DictLoader

from extensions import db
from messages import messages_bp
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
        cls.app.register_blueprint(messages_bp)
        cls.app.jinja_loader = ChoiceLoader([
            DictLoader({
                "portal/layout.html": (
                    "<!doctype html>{% block content %}{% endblock %}"
                    "{% block scripts %}{% endblock %}"
                ),
                "layout.html": (
                    "<!doctype html>{% block title %}{% endblock %}"
                    "{% block content %}{% endblock %}"
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
            self.assertIn("عنوان الإشعار", form_response.get_data(as_text=True))
            self.assertIn("نص الرسالة التفصيلي", form_response.get_data(as_text=True))

            response = client.post(
                "/portal/admin/notifications/send",
                data={
                    "title": "مراجعة النظام اليوم",
                    "body": "<p>يرجى مراجعة <strong>النظام</strong> اليوم.</p>",
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
        internal_message = Message.query.one()
        self.assertEqual({row.user_id for row in rows}, {
            self.department_employee.id,
            self.other_employee.id,
        })
        self.assertTrue(all(row.source == "portal" for row in rows))
        self.assertTrue(all(row.type == "URGENT" for row in rows))
        self.assertTrue(all(row.is_visible is True for row in rows))
        self.assertEqual({row.message for row in rows}, {"مراجعة النظام اليوم"})
        self.assertTrue(all(
            row.link_url == f"/messages/view/{internal_message.id}"
            for row in rows
        ))
        self.assertEqual(internal_message.target_kind, "ADMIN_BROADCAST")
        self.assertEqual(internal_message.subject, "مراجعة النظام اليوم")
        self.assertIn("<strong>النظام</strong>", internal_message.body)
        self.assertEqual(MessageRecipient.query.filter_by(message_id=internal_message.id).count(), 2)
        self.assertTrue(all(row.actor_id == self.admin.id for row in rows))
        self.assertEqual(len({row.event_key for row in rows}), 1)
        self.assertEqual(
            AuditLog.query.filter_by(action="PORTAL_ADMIN_NOTIFICATION_SEND").count(),
            1,
        )

        with self.app.test_client() as client:
            self._login(client, self.department_employee.id)
            notification_inbox = client.get("/portal/notifications")
            with patch("messages.routes.render_template", return_value="broadcast message") as render:
                message_view = client.get(f"/messages/view/{internal_message.id}")
            denied = client.get("/portal/admin/notifications/send")
        self.assertEqual(notification_inbox.status_code, 200)
        self.assertIn("مراجعة النظام اليوم", notification_inbox.get_data(as_text=True))
        self.assertEqual(message_view.status_code, 200)
        self.assertEqual(
            render.call_args.kwargs["feedback_url"],
            f"/portal/trouble-tickets/feedback/{next(row.id for row in rows if row.user_id == self.department_employee.id)}",
        )
        self.assertEqual(denied.status_code, 403)

    def test_super_admin_can_bulk_delete_many_selected_notifications(self):
        selected = [
            Notification(
                user_id=self.other_employee.id,
                message=f"Bulk notification {index}",
                type="INFO",
                source="portal",
                is_visible=True,
                is_read=False,
            )
            for index in range(3)
        ]
        untouched = Notification(
            user_id=self.other_employee.id,
            message="Keep this notification",
            type="INFO",
            source="portal",
            is_visible=True,
            is_read=False,
        )
        db.session.add_all([*selected, untouched])
        db.session.commit()
        selected_ids = [notification.id for notification in selected]

        with self.app.test_client() as client:
            self._login(client, self.super_admin.id)
            page = client.get(
                f"/portal/admin/notifications?user_id={self.other_employee.id}"
            )
            response = client.post(
                "/portal/admin/notifications",
                data={
                    "target_user_id": str(self.other_employee.id),
                    "notification_ids": [str(value) for value in selected_ids],
                },
            )

        self.assertEqual(page.status_code, 200)
        self.assertIn("Shift +", page.get_data(as_text=True))
        self.assertIn("Ctrl+A", page.get_data(as_text=True))
        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        for notification_id in selected_ids:
            notification = db.session.get(Notification, notification_id)
            self.assertFalse(notification.is_visible)
            self.assertTrue(notification.is_read)
        kept = db.session.get(Notification, untouched.id)
        self.assertTrue(kept.is_visible)
        self.assertFalse(kept.is_read)

    def test_rich_broadcast_body_is_sanitized_and_keeps_safe_formatting(self):
        with self.app.test_client() as client:
            self._login(client, self.admin.id)
            response = client.post(
                "/portal/admin/notifications/send",
                data={
                    "title": "تحديث واجهة النظام",
                    "body": (
                        '<h3 style="color:#0d6efd;background-color:rgb(255, 243, 205)">عنوان منسق</h3>'
                        '<p><strong>نص مهم</strong> <a href="https://example.test">رابط</a></p>'
                        '<script>alert(1)</script>'
                        '<a href="javascript:alert(1)">رابط غير آمن</a>'
                    ),
                    "level": "INFO",
                    "target_scope": "USERS",
                    "recipient_user_ids": [str(self.department_employee.id)],
                },
            )
        self.assertEqual(response.status_code, 302)

        notification = Notification.query.filter_by(user_id=self.department_employee.id).one()
        internal_message = Message.query.one()
        self.assertEqual(notification.message, "تحديث واجهة النظام")
        self.assertEqual(internal_message.subject, "تحديث واجهة النظام")
        self.assertIn('style="color: #0d6efd;', internal_message.body)
        self.assertIn("background-color: rgb(255, 243, 205)", internal_message.body)
        self.assertIn('<strong>نص مهم</strong>', internal_message.body)
        self.assertIn('href="https://example.test"', internal_message.body)
        self.assertNotIn("script", internal_message.body.lower())
        self.assertNotIn("javascript:", internal_message.body.lower())

        with self.app.test_client() as client:
            self._login(client, self.department_employee.id)
            with patch("messages.routes.render_template", return_value="broadcast message") as render:
                opened = client.get(f"/messages/view/{internal_message.id}")
        self.assertEqual(opened.status_code, 200)
        rendered_body = str(render.call_args.kwargs["broadcast_body_html"])
        self.assertIn('<h3 style="color: #0d6efd; background-color: rgb(255, 243, 205)">عنوان منسق</h3>', rendered_body)
        self.assertNotIn("script", rendered_body.lower())

    def test_existing_and_new_broadcasts_show_the_system_sender_label(self):
        with self.app.test_client() as client:
            self._login(client, self.admin.id)
            sent = client.post(
                "/portal/admin/notifications/send",
                data={
                    "title": "تحديث باسم المرسل النظامي",
                    "body": "تظهر الرسالة باسم النظام.",
                    "level": "INFO",
                    "target_scope": "USERS",
                    "recipient_user_ids": [str(self.department_employee.id)],
                },
            )
        self.assertEqual(sent.status_code, 302)
        message = Message.query.one()

        with self.app.test_client() as client:
            self._login(client, self.department_employee.id)
            inbox = client.get("/messages/inbox")
            message_view = client.get(f"/messages/view/{message.id}")
        self.assertEqual(inbox.status_code, 200)
        self.assertEqual(message_view.status_code, 200)
        for response in (inbox, message_view):
            content = response.get_data(as_text=True)
            self.assertIn("نظام مسار والبوابة الإدارية", content)
            self.assertNotIn(self.admin.email, content)

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
        internal_message = Message.query.one()
        self.assertEqual(row.target_id, self.linked_outbound.id)
        self.assertEqual(row.link_url, f"/messages/view/{internal_message.id}")
        self.assertTrue(row.is_visible)
        self.assertFalse(internal_message.is_system_generated)
        self.assertEqual(internal_message.target_kind, "ADMIN_BROADCAST")
        self.assertEqual(internal_message.subject, "تمت إضافة ميزة جديدة. راجع الدليل المرتبط.")
        self.assertEqual(internal_message.body, "تمت إضافة ميزة جديدة. راجع الدليل المرتبط.")
        self.assertEqual(
            MessageRecipient.query.filter_by(
                message_id=internal_message.id,
                recipient_user_id=self.department_employee.id,
                is_read=False,
            ).count(),
            1,
        )

        with self.app.test_client() as client:
            self._login(client, self.department_employee.id)
            opened = client.get(f"/portal/notifications/{row.id}/open")
            self.assertEqual(opened.status_code, 302)
            self.assertEqual(opened.headers["Location"], row.link_url)
            with patch("messages.routes.render_template", return_value="broadcast message") as render:
                message_view = client.get(opened.headers["Location"])
            self.assertEqual(message_view.status_code, 200)
            self.assertEqual(render.call_args.args[0], "messages/view.html")
            self.assertTrue(render.call_args.kwargs["is_admin_broadcast_message"])
            self.assertEqual(
                render.call_args.kwargs["feedback_url"],
                f"/portal/trouble-tickets/feedback/{row.id}",
            )
            reply = client.get(f"/messages/reply/{internal_message.id}")
        self.assertEqual(reply.status_code, 302)
        self.assertEqual(reply.headers["Location"], row.link_url)

    def test_recipient_can_submit_feedback_on_a_linked_update(self):
        # Receiving the update creates a safe inbox correspondence.  Feedback
        # does not depend on the recipient having direct access to the original
        # correspondence selected by the administrator as a reference.

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
        internal_message = Message.query.one()

        with self.app.test_client() as client:
            self._login(client, self.department_employee.id)
            opened = client.get(f"/portal/notifications/{notification.id}/open")
            self.assertEqual(opened.status_code, 302)
            self.assertEqual(
                opened.headers["Location"],
                f"/messages/view/{internal_message.id}",
            )

            with patch("messages.routes.render_template", return_value="feedback view") as render:
                message_view = client.get(opened.headers["Location"])
            self.assertEqual(message_view.status_code, 200)
            self.assertEqual(render.call_args.args[0], "messages/view.html")
            self.assertEqual(
                render.call_args.kwargs["feedback_url"],
                f"/portal/trouble-tickets/feedback/{notification.id}",
            )

            form = client.get(f"/portal/trouble-tickets/feedback/{notification.id}")
            self.assertEqual(form.status_code, 200)
            self.assertIn("إرسال ملاحظات حول تحديث النظام", form.get_data(as_text=True))
            self.assertIn("مراسلة مرتبطة بتحديث النظام", form.get_data(as_text=True))
            self.assertNotIn(self.linked_outbound.subject, form.get_data(as_text=True))
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
        self.assertIn(f"إشعار #{notification.id}", ticket.subject)
        self.assertNotIn("محاولة تغيير عنوان التذكرة", ticket.subject)
        self.assertIn("أقترح شرحاً أوضح لإعدادات الخصوصية.", ticket.description)
        self.assertIn(f"مرجع الإشعار: #{notification.id}", ticket.description)
        self.assertIn(f"مرجع المراسلة: OUT:{self.linked_outbound.id}", ticket.description)
        self.assertIn(f"/messages/view/{internal_message.id}", ticket.description)
        self.assertIsNotNone(AuditLog.query.filter_by(
            action="TROUBLE_TICKET_FEEDBACK_CREATE",
            target_id=ticket.id,
        ).first())

        with self.app.test_client() as client:
            self._login(client, self.admin.id)
            ticket_view = client.get(f"/portal/trouble-tickets/{ticket.id}")
            notification_reference = client.get(
                f"/portal/trouble-tickets/{ticket.id}/notification-reference"
            )
        self.assertEqual(ticket_view.status_code, 200)
        self.assertIn(
            f"/portal/trouble-tickets/{ticket.id}/notification-reference",
            ticket_view.get_data(as_text=True),
        )
        self.assertEqual(notification_reference.status_code, 200)
        reference_body = notification_reference.get_data(as_text=True)
        self.assertIn(f"الإشعار #{notification.id}", reference_body)
        self.assertIn(internal_message.subject, reference_body)
        self.assertIn(internal_message.body, reference_body)

        with self.app.test_client() as client:
            self._login(client, self.other_employee.id)
            forbidden_feedback = client.get(
                f"/portal/trouble-tickets/feedback/{notification.id}"
            )
            forbidden_reference = client.get(
                f"/portal/trouble-tickets/{ticket.id}/notification-reference"
            )
        self.assertEqual(forbidden_feedback.status_code, 404)
        self.assertEqual(forbidden_reference.status_code, 403)

    def test_admin_can_silently_edit_own_broadcast_without_redelivery(self):
        with self.app.test_client() as client:
            self._login(client, self.admin.id)
            sent = client.post(
                "/portal/admin/notifications/send",
                data={
                    "title": "عنوان قبل التعديل",
                    "body": "<p>النص قبل التعديل.</p>",
                    "level": "INFO",
                    "target_scope": "USERS",
                    "recipient_user_ids": [
                        str(self.department_employee.id),
                        str(self.other_employee.id),
                    ],
                },
            )
        self.assertEqual(sent.status_code, 302)

        message = Message.query.one()
        notifications = Notification.query.order_by(Notification.id.asc()).all()
        recipients = MessageRecipient.query.order_by(MessageRecipient.id.asc()).all()
        notification_count = len(notifications)
        recipient_count = len(recipients)
        first_notification_id = notifications[0].id
        first_notification_created_at = notifications[0].created_at
        first_notification_event_key = notifications[0].event_key
        notifications[0].is_read = True
        recipients[0].is_read = True
        db.session.commit()

        with self.app.test_client() as client:
            self._login(client, self.admin.id)
            edit_form = client.get(
                f"/portal/admin/notifications/sent/{message.id}/edit"
            )
            edited = client.post(
                f"/portal/admin/notifications/sent/{message.id}/edit",
                data={
                    "title": "عنوان بعد التعديل",
                    "body": (
                        '<p><strong>النص بعد التعديل</strong></p>'
                        '<script>alert(1)</script>'
                    ),
                },
            )
        self.assertEqual(edit_form.status_code, 200)
        self.assertIn("تعديل إشعار مُرسل", edit_form.get_data(as_text=True))
        self.assertEqual(edited.status_code, 302)

        db.session.expire_all()
        updated_message = db.session.get(Message, message.id)
        updated_notifications = Notification.query.order_by(Notification.id.asc()).all()
        updated_recipients = MessageRecipient.query.order_by(MessageRecipient.id.asc()).all()
        updated_first_notification = db.session.get(Notification, first_notification_id)
        self.assertEqual(Notification.query.count(), notification_count)
        self.assertEqual(MessageRecipient.query.count(), recipient_count)
        self.assertEqual(updated_message.subject, "عنوان بعد التعديل")
        self.assertIn("<strong>النص بعد التعديل</strong>", updated_message.body)
        self.assertNotIn("script", updated_message.body.lower())
        self.assertEqual(
            {notification.message for notification in updated_notifications},
            {"عنوان بعد التعديل"},
        )
        self.assertTrue(updated_first_notification.is_read)
        self.assertEqual(updated_first_notification.created_at, first_notification_created_at)
        self.assertEqual(updated_first_notification.event_key, first_notification_event_key)
        self.assertTrue(updated_recipients[0].is_read)
        self.assertIsNotNone(AuditLog.query.filter_by(
            action="PORTAL_ADMIN_BROADCAST_EDIT",
            target_id=message.id,
        ).first())

    def test_deleting_sender_copy_keeps_the_broadcast_visible_to_recipients(self):
        with self.app.test_client() as client:
            self._login(client, self.admin.id)
            sent = client.post(
                "/portal/admin/notifications/send",
                data={
                    "title": "إشعار يحذف من المرسلة",
                    "body": "يبقى هذا النص عند المستلمين.",
                    "level": "INFO",
                    "target_scope": "USERS",
                    "recipient_user_ids": [str(self.department_employee.id)],
                },
            )
        self.assertEqual(sent.status_code, 302)
        message = Message.query.one()

        with self.app.test_client() as client:
            self._login(client, self.admin.id)
            sent_list = client.get("/portal/admin/notifications/sent")
            deleted = client.post(
                "/portal/admin/notifications/sent",
                data={
                    "message_id": str(message.id),
                    "action": "DELETE_SENT_COPY",
                },
            )
            sent_list_after = client.get("/portal/admin/notifications/sent")
            hidden_edit = client.get(
                f"/portal/admin/notifications/sent/{message.id}/edit"
            )
        with self.app.test_client() as client:
            self._login(client, self.super_admin.id)
            super_sent_list_after = client.get("/portal/admin/notifications/sent")
            hidden_edit_for_super_admin = client.get(
                f"/portal/admin/notifications/sent/{message.id}/edit"
            )
        self.assertEqual(sent_list.status_code, 200)
        self.assertIn("إشعار يحذف من المرسلة", sent_list.get_data(as_text=True))
        self.assertEqual(deleted.status_code, 302)
        self.assertEqual(sent_list_after.status_code, 200)
        self.assertNotIn("إشعار يحذف من المرسلة", sent_list_after.get_data(as_text=True))
        self.assertEqual(hidden_edit.status_code, 404)
        self.assertEqual(super_sent_list_after.status_code, 200)
        self.assertNotIn(
            "إشعار يحذف من المرسلة",
            super_sent_list_after.get_data(as_text=True),
        )
        self.assertEqual(hidden_edit_for_super_admin.status_code, 404)

        db.session.expire_all()
        self.assertTrue(db.session.get(Message, message.id).sender_deleted)
        self.assertTrue(Notification.query.one().is_visible)
        self.assertFalse(MessageRecipient.query.one().is_deleted)
        self.assertIsNotNone(AuditLog.query.filter_by(
            action="PORTAL_ADMIN_BROADCAST_DELETE_SENT_COPY",
            target_id=message.id,
        ).first())

    def test_super_admin_can_delete_recipient_copies_but_admin_cannot_manage_others(self):
        with self.app.test_client() as client:
            self._login(client, self.super_admin.id)
            sent = client.post(
                "/portal/admin/notifications/send",
                data={
                    "title": "إشعار السوبر أدمن",
                    "body": "هذا النص سيحذف من حسابات المستلمين.",
                    "level": "URGENT",
                    "target_scope": "USERS",
                    "recipient_user_ids": [
                        str(self.department_employee.id),
                        str(self.other_employee.id),
                    ],
                },
            )
        self.assertEqual(sent.status_code, 302)
        message = Message.query.one()

        with self.app.test_client() as client:
            self._login(client, self.admin.id)
            foreign_edit = client.get(
                f"/portal/admin/notifications/sent/{message.id}/edit"
            )
            foreign_delete = client.post(
                "/portal/admin/notifications/sent",
                data={
                    "message_id": str(message.id),
                    "action": "DELETE_RECIPIENT_COPIES",
                },
            )
        self.assertEqual(foreign_edit.status_code, 403)
        self.assertEqual(foreign_delete.status_code, 403)

        with self.app.test_client() as client:
            self._login(client, self.super_admin.id)
            deleted = client.post(
                "/portal/admin/notifications/sent",
                data={
                    "message_id": str(message.id),
                    "action": "DELETE_RECIPIENT_COPIES",
                },
            )
        self.assertEqual(deleted.status_code, 302)

        db.session.expire_all()
        self.assertFalse(db.session.get(Message, message.id).sender_deleted)
        self.assertTrue(all(
            notification.is_visible is False and notification.is_read is True
            for notification in Notification.query.all()
        ))
        self.assertTrue(all(
            recipient.is_deleted is True
            for recipient in MessageRecipient.query.all()
        ))
        self.assertIsNotNone(AuditLog.query.filter_by(
            action="PORTAL_ADMIN_BROADCAST_DELETE_RECIPIENT_COPIES",
            target_id=message.id,
        ).first())


if __name__ == "__main__":
    unittest.main()
