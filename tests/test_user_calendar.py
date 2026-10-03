import unittest
from datetime import datetime
from pathlib import Path

from flask import Flask, g
from flask_login import LoginManager
from jinja2 import ChoiceLoader, DictLoader

from extensions import db
from models import (
    PortalMeeting,
    PortalMeetingParticipant,
    PortalMeetingTask,
    User,
    UserCalendarEvent,
    WorkflowInstance,
    WorkflowInstanceStep,
    WorkflowRequest,
)
from portal import portal_bp
from workflow import workflow_bp


class UserCalendarTests(unittest.TestCase):
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
            SECRET_KEY="user-calendar-test",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            APP_TIMEZONE="Asia/Jerusalem",
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
        cls.app.register_blueprint(workflow_bp)
        cls.app.jinja_loader = ChoiceLoader([
            DictLoader({"layout.html": "{% block content %}{% endblock %}"}),
            cls.app.jinja_loader,
        ])
        cls.app.jinja_env.globals["csrf_token"] = lambda: "test-token"
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

        self.owner = User(email="owner@example.test", name="Owner", password_hash="x", role="EMPLOYEE")
        self.other = User(email="other@example.test", name="Other", password_hash="x", role="EMPLOYEE")
        self.organizer = User(email="organizer@example.test", name="Organizer", password_hash="x", role="EMPLOYEE")
        db.session.add_all((self.owner, self.other, self.organizer))
        db.session.flush()

        self.meeting = PortalMeeting(
            title="اجتماع تقويمي خاص",
            start_at=datetime(2026, 10, 21, 10, 0),
            created_by_user_id=self.organizer.id,
        )
        db.session.add(self.meeting)
        db.session.flush()
        db.session.add(PortalMeetingParticipant(
            meeting_id=self.meeting.id,
            user_id=self.owner.id,
            role="ATTENDEE",
            attendance_status="INVITED",
        ))
        db.session.add(PortalMeetingTask(
            meeting_id=self.meeting.id,
            title="مهمة متابعة تقويمية",
            assignee_user_id=self.owner.id,
            due_date=datetime(2026, 10, 22).date(),
            status="OPEN",
            created_by_user_id=self.organizer.id,
        ))
        db.session.add(UserCalendarEvent(
            owner_user_id=self.other.id,
            title="موعد مستخدم آخر",
            event_type="PERSONAL",
            start_at=datetime(2026, 10, 20, 8, 0),
            all_day=False,
        ))

        self.workflow_request = WorkflowRequest(
            title="مسار للتقويم",
            description="وصف المسار للتقويم",
            status="IN_PROGRESS",
            requester_id=self.owner.id,
            confidentiality="NORMAL",
        )
        db.session.add(self.workflow_request)
        db.session.flush()
        instance = WorkflowInstance(
            request_id=self.workflow_request.id,
            current_step_order=1,
        )
        db.session.add(instance)
        db.session.flush()
        db.session.add(WorkflowInstanceStep(
            instance_id=instance.id,
            step_order=1,
            approver_kind="USER",
            approver_user_id=self.owner.id,
            status="PENDING",
            due_at=datetime(2026, 10, 23, 8, 0),
        ))
        db.session.commit()

    def _login(self, client, user_id):
        g.pop("_login_user", None)
        with client.session_transaction() as session:
            session.clear()
            session["_user_id"] = str(user_id)
            session["_fresh"] = True

    def test_private_event_is_owned_and_editable_only_by_its_creator(self):
        client = self.app.test_client()
        self._login(client, self.owner.id)

        response = client.post(
            "/portal/calendar/new",
            data={
                "title": "موعد خاص",
                "event_type": "REVIEW",
                "event_date": "2026-10-20",
                "all_day": "1",
                "description": "مراجعة خاصة",
            },
        )
        self.assertEqual(response.status_code, 302)
        row = UserCalendarEvent.query.filter_by(title="موعد خاص").one()
        self.assertEqual(row.owner_user_id, self.owner.id)
        self.assertEqual(row.event_type, "REVIEW")
        self.assertEqual(row.reminder_minutes_before, 1440)

        self._login(client, self.other.id)
        self.assertEqual(
            client.post(
                f"/portal/calendar/{row.id}/edit",
                data={
                    "title": "محاولة تعديل",
                    "event_type": "PERSONAL",
                    "event_date": "2026-10-20",
                    "all_day": "1",
                },
            ).status_code,
            404,
        )
        self.assertEqual(db.session.get(UserCalendarEvent, row.id).title, "موعد خاص")

    def test_calendar_aggregates_only_visible_meetings_and_my_tasks(self):
        client = self.app.test_client()
        self._login(client, self.owner.id)

        response = client.get("/portal/calendar?month=2026-10")
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.meeting.title.encode("utf-8"), response.data)
        self.assertIn("مهمة متابعة تقويمية".encode("utf-8"), response.data)
        self.assertNotIn("موعد مستخدم آخر".encode("utf-8"), response.data)

    def test_calendar_can_jump_to_an_explicit_day_month_and_year(self):
        client = self.app.test_client()
        self._login(client, self.owner.id)

        response = client.get("/portal/calendar?date=2028-04-15")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'value="2028-04-15"', response.data)
        self.assertIn(b"calendar-selected", response.data)
        self.assertIn(b"/portal/calendar/new?date=2028-04-15", response.data)

        form_response = client.get("/portal/calendar/new?date=2028-04-15")
        self.assertEqual(form_response.status_code, 200)
        self.assertIn(b'name="reminder_minutes_before"', form_response.data)

    def test_workflow_shortcut_creates_a_private_linked_event(self):
        client = self.app.test_client()
        self._login(client, self.owner.id)

        response = client.post(
            f"/workflow/request/{self.workflow_request.id}/calendar",
            data={
                "title": "متابعة المسار",
                "event_date": "2026-10-23",
                "all_day": "1",
                "description": "تمت الإضافة من المسار",
                "return_to": "/portal/calendar",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/portal/calendar")
        row = UserCalendarEvent.query.filter_by(title="متابعة المسار").one()
        self.assertEqual(row.owner_user_id, self.owner.id)
        self.assertEqual(row.event_type, "WORKFLOW")
        self.assertEqual(row.reminder_minutes_before, 1440)
        self.assertEqual(row.workflow_request_id, self.workflow_request.id)
        self.assertEqual(row.workflow_step_order, 1)

        self._login(client, self.other.id)
        denied = client.post(
            f"/workflow/request/{self.workflow_request.id}/calendar",
            data={
                "title": "محاولة غير مصرح بها",
                "event_date": "2026-10-23",
                "all_day": "1",
            },
        )
        self.assertEqual(denied.status_code, 403)


if __name__ == "__main__":
    unittest.main()
