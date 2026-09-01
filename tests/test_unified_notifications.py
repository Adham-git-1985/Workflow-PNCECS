import tempfile
import unittest
from pathlib import Path

from flask import Flask, g
from flask_login import LoginManager
from jinja2 import ChoiceLoader, DictLoader

from extensions import db
from models import Notification, PortalCircular, TroubleTicket, User, UserPermission
from portal import portal_bp
from utils.events import emit_event
from utils.notification_links import notification_target_path, safe_local_notification_url
from workflow import workflow_bp


class UnifiedNotificationRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        project_root = Path(__file__).resolve().parents[1]
        cls.app = Flask(
            __name__,
            instance_path=cls.temp_dir.name,
            template_folder=str(project_root / "templates"),
            static_folder=str(project_root / "static"),
        )
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="unified-notification-test",
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

        cls.app.register_blueprint(workflow_bp)
        cls.app.register_blueprint(portal_bp)
        cls.app.jinja_loader = ChoiceLoader([
            DictLoader({
                "layout.html": "{% block content %}{% endblock %}",
                "portal/layout.html": "{% block content %}{% endblock %}",
            }),
            cls.app.jinja_loader,
        ])
        cls.app.jinja_env.filters["ui_label"] = lambda value: value
        cls.context = cls.app.app_context()
        cls.context.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.context.pop()
        cls.temp_dir.cleanup()

    def setUp(self):
        db.session.remove()
        db.drop_all()
        db.create_all()
        self.user = User(
            email="notifications@example.test",
            name="Notifications User",
            password_hash="not-used-in-test",
            role="SUPER_ADMIN",
        )
        self.other_user = User(
            email="other-notifications@example.test",
            name="Other User",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        db.session.add_all((self.user, self.other_user))
        db.session.commit()

    def _login(self, client, user_id):
        g.pop("_login_user", None)
        with client.session_transaction() as session:
            session.clear()
            session["_user_id"] = str(user_id)
            session["_fresh"] = True

    def test_masar_inbox_includes_workflow_and_portal_notifications(self):
        workflow_notification = Notification(
            user_id=self.user.id,
            message="تحديث طلب مسار",
            source="workflow",
            link_url="/workflow/request/81",
            is_read=False,
        )
        portal_notification = Notification(
            user_id=self.user.id,
            message="تذكرة دعم جديدة #42",
            source="portal",
            type="PORTAL",
            link_url="/portal/trouble-tickets/42",
            is_read=False,
        )
        db.session.add_all((workflow_notification, portal_notification))
        db.session.commit()

        with self.app.test_client() as client:
            self._login(client, self.user.id)
            response = client.get("/workflow/notifications")
            count_response = client.get("/workflow/notifications/unread-count")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("تحديث طلب مسار", body)
        self.assertIn("تذكرة دعم جديدة #42", body)
        self.assertIn("البوابة الإدارية", body)
        self.assertIn(f"/workflow/notifications/{portal_notification.id}/open", body)
        self.assertEqual(count_response.get_json(), {"count": 2})

    def test_open_notification_marks_portal_row_read_and_redirects_to_ticket(self):
        notification = Notification(
            user_id=self.user.id,
            message="تذكرة دعم جديدة #73",
            source="portal",
            type="PORTAL",
            link_url="/portal/trouble-tickets/73",
            is_read=False,
        )
        db.session.add(notification)
        db.session.commit()
        notification_id = notification.id

        with self.app.test_client() as client:
            self._login(client, self.user.id)
            response = client.get(f"/workflow/notifications/{notification_id}/open")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/portal/trouble-tickets/73")
        self.assertTrue(db.session.get(Notification, notification_id).is_read)

    def test_legacy_ticket_number_is_linked_without_matching_a_workflow_request(self):
        notification = Notification(
            user_id=self.user.id,
            message="تم تحديث تذكرة الدعم #19",
            source="portal",
            type="PORTAL",
            is_read=False,
        )
        db.session.add(notification)
        db.session.commit()

        with self.app.test_client() as client:
            self._login(client, self.user.id)
            response = client.get(f"/workflow/notifications/{notification.id}/open")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/portal/trouble-tickets/19")

    def test_mark_all_read_includes_portal_notifications(self):
        db.session.add_all((
            Notification(user_id=self.user.id, message="Workflow", source="workflow", is_read=False),
            Notification(user_id=self.user.id, message="Portal", source="portal", is_read=False),
            Notification(user_id=self.other_user.id, message="Other", source="portal", is_read=False),
        ))
        db.session.commit()

        with self.app.test_client() as client:
            self._login(client, self.user.id)
            response = client.post("/workflow/notifications/mark-all-read")

        self.assertEqual(response.status_code, 302)
        own_rows = Notification.query.filter_by(user_id=self.user.id).all()
        self.assertTrue(all(row.is_read for row in own_rows))
        self.assertFalse(Notification.query.filter_by(user_id=self.other_user.id).one().is_read)

    def test_opening_circular_clears_its_portal_notification(self):
        circular = PortalCircular(
            title="تعميم موحد",
            body="محتوى التعميم",
            target_scope="ALL",
            is_active=True,
            created_by_user_id=self.user.id,
        )
        db.session.add(circular)
        db.session.flush()
        notification = Notification(
            user_id=self.user.id,
            message=f"تعميم جديد: {circular.title}",
            source="portal",
            type="INFO",
            link_url=f"/portal/circulars/{circular.id}",
            is_read=False,
        )
        db.session.add(notification)
        db.session.commit()
        notification_id = notification.id

        with self.app.test_client() as client:
            self._login(client, self.user.id)
            response = client.get(f"/portal/circulars/{circular.id}")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(db.session.get(Notification, notification_id).is_read)

    def test_new_support_ticket_notifies_admin_with_a_direct_link(self):
        with self.app.test_client() as client:
            self._login(client, self.other_user.id)
            response = client.post(
                "/portal/trouble-tickets/new",
                data={
                    "subject": "مشكلة في الدخول",
                    "description": "لا أستطيع فتح النظام",
                    "category": "SYSTEM",
                    "priority": "HIGH",
                },
            )

        self.assertEqual(response.status_code, 302)
        ticket = TroubleTicket.query.one()
        notification = Notification.query.filter_by(user_id=self.user.id, source="portal").one()
        self.assertIn(f"#{ticket.id}", notification.message)
        self.assertEqual(notification.link_url, f"/portal/trouble-tickets/{ticket.id}")

    def test_support_tickets_and_updates_are_limited_to_requester_and_admin_roles(self):
        admin = User(
            email="admin@example.test",
            name="Admin",
            password_hash="not-used-in-test",
            role="ADMIN",
        )
        support_agent = User(
            email="support-agent@example.test",
            name="Support Agent",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        db.session.add_all((admin, support_agent))
        db.session.flush()
        db.session.add(UserPermission(
            user_id=support_agent.id,
            key="TROUBLE_TICKETS_MANAGE",
            is_allowed=True,
        ))
        ticket = TroubleTicket(
            requester_id=self.other_user.id,
            assigned_to_id=support_agent.id,
            subject="Restricted support ticket",
            description="Only the requester and administrators may view this.",
            category="SYSTEM",
            priority="HIGH",
            status="OPEN",
        )
        db.session.add(ticket)
        db.session.commit()

        with self.app.test_client() as client:
            self._login(client, support_agent.id)
            list_response = client.get("/portal/trouble-tickets?scope=all")
            detail_response = client.get(f"/portal/trouble-tickets/{ticket.id}")
            manage_response = client.get("/portal/trouble-tickets/manage")

        self.assertEqual(list_response.status_code, 200)
        self.assertNotIn("Restricted support ticket", list_response.get_data(as_text=True))
        self.assertEqual(detail_response.status_code, 403)
        self.assertEqual(manage_response.status_code, 403)

        with self.app.test_client() as client:
            self._login(client, admin.id)
            admin_detail_response = client.get(f"/portal/trouble-tickets/{ticket.id}")

        self.assertEqual(admin_detail_response.status_code, 200)

        with self.app.test_client() as client:
            self._login(client, self.other_user.id)
            response = client.post(
                f"/portal/trouble-tickets/{ticket.id}",
                data={"action": "comment", "body": "Please review the latest details."},
            )

        self.assertEqual(response.status_code, 302)
        comment_recipients = {
            notification.user_id
            for notification in Notification.query.filter_by(type="TROUBLE_TICKET").all()
        }
        self.assertEqual(comment_recipients, {self.user.id, admin.id})

        with self.app.test_client() as client:
            self._login(client, self.user.id)
            response = client.post(
                f"/portal/trouble-tickets/{ticket.id}",
                data={"action": "update", "status": "RESOLVED", "assigned_to_id": ""},
            )

        self.assertEqual(response.status_code, 302)
        update_recipients = {
            notification.user_id
            for notification in Notification.query.filter_by(type="TROUBLE_TICKET").all()
            if "تم تحديث تذكرة الدعم" in notification.message
        }
        self.assertEqual(update_recipients, {self.other_user.id, admin.id})

    def test_support_ticket_search_matches_requester_email(self):
        matching_ticket = TroubleTicket(
            requester_id=self.other_user.id,
            subject="Printer connection issue",
            description="The office printer is unavailable.",
            category="HARDWARE",
            priority="NORMAL",
            status="OPEN",
        )
        non_matching_ticket = TroubleTicket(
            requester_id=self.user.id,
            subject="Network access issue",
            description="The network connection is slow.",
            category="NETWORK",
            priority="NORMAL",
            status="OPEN",
        )
        db.session.add_all((matching_ticket, non_matching_ticket))
        db.session.commit()

        with self.app.test_client() as client:
            self._login(client, self.user.id)
            response = client.get("/portal/trouble-tickets?scope=all&q=other-notifications%40example.test")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Printer connection issue", body)
        self.assertNotIn("Network access issue", body)

    def test_support_ticket_management_search_preserves_status_filter(self):
        matching_ticket = TroubleTicket(
            requester_id=self.other_user.id,
            subject="Scanner attachment is unavailable",
            description="The scanner cannot upload a PDF.",
            category="HARDWARE",
            priority="HIGH",
            status="OPEN",
        )
        non_matching_ticket = TroubleTicket(
            requester_id=self.other_user.id,
            subject="Scanner attachment is unavailable",
            description="The scanner cannot upload a PDF.",
            category="HARDWARE",
            priority="HIGH",
            status="CLOSED",
        )
        db.session.add_all((matching_ticket, non_matching_ticket))
        db.session.commit()

        with self.app.test_client() as client:
            self._login(client, self.user.id)
            response = client.get("/portal/trouble-tickets/manage?status=OPEN&q=scanner")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn(f"#{matching_ticket.id}", body)
        self.assertNotIn(f"#{non_matching_ticket.id}", body)

    def test_workflow_events_store_the_request_destination(self):
        emit_event(
            actor_id=self.user.id,
            action="REQUEST_UPDATE",
            message="تم تحديث الطلب #33",
            target_type="WorkflowRequest",
            target_id=33,
            notify_user_id=self.other_user.id,
            auto_commit=False,
        )
        db.session.commit()

        notification = Notification.query.filter_by(user_id=self.other_user.id).one()
        self.assertEqual(notification.link_url, "/workflow/request/33")


class NotificationLinkHelperTests(unittest.TestCase):
    def test_target_paths_cover_workflow_ticket_and_portal_requests(self):
        self.assertEqual(notification_target_path("WorkflowRequest", 5), "/workflow/request/5")
        self.assertEqual(notification_target_path("TROUBLE_TICKET", 7), "/portal/trouble-tickets/7")
        self.assertEqual(
            notification_target_path("HR_SS_REQUEST", 9),
            "/portal/hr/self-service/requests/9",
        )

    def test_external_notification_links_are_rejected(self):
        self.assertIsNone(safe_local_notification_url("https://example.test/request/1"))
        self.assertIsNone(safe_local_notification_url("//example.test/request/1"))
        self.assertEqual(safe_local_notification_url("/portal/request/1"), "/portal/request/1")


if __name__ == "__main__":
    unittest.main()
