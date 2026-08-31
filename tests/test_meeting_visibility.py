import unittest
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, g
from flask_login import LoginManager
from jinja2 import ChoiceLoader, DictLoader

from extensions import db
from models import PortalMeeting, PortalMeetingParticipant, PortalMeetingTask, User, UserPermission
from portal import portal_bp


class MeetingVisibilityTests(unittest.TestCase):
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
            SECRET_KEY="meeting-visibility-test",
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

        self.organizer = User(email="organizer@example.test", name="Organizer", password_hash="x")
        self.invitee = User(email="invitee@example.test", name="Invitee", password_hash="x")
        self.outsider = User(email="outsider@example.test", name="Outsider", password_hash="x")
        self.meetings_admin = User(email="meetings-admin@example.test", name="Meetings Admin", password_hash="x")
        self.admin = User(email="admin@example.test", name="Admin", password_hash="x", role="ADMIN")
        self.super_admin = User(email="super-admin@example.test", name="Super Admin", password_hash="x", role="SUPER_ADMIN")
        db.session.add_all((
            self.organizer,
            self.invitee,
            self.outsider,
            self.meetings_admin,
            self.admin,
            self.super_admin,
        ))
        db.session.flush()
        db.session.add_all([
            UserPermission(user_id=user.id, key="PORTAL_READ", is_allowed=True)
            for user in (
                self.organizer,
                self.invitee,
                self.outsider,
                self.meetings_admin,
                self.admin,
                self.super_admin,
            )
        ])
        db.session.add(UserPermission(
            user_id=self.organizer.id,
            key="PORTAL_MEETINGS_MANAGE",
            is_allowed=True,
        ))
        db.session.add(UserPermission(
            user_id=self.meetings_admin.id,
            key="PORTAL_MEETINGS_MANAGE",
            is_allowed=True,
        ))

        self.meeting = PortalMeeting(
            title="Private strategy meeting",
            start_at=datetime.utcnow() + timedelta(days=1),
            created_by_user_id=self.organizer.id,
        )
        db.session.add(self.meeting)
        db.session.flush()
        db.session.add(PortalMeetingParticipant(
            meeting_id=self.meeting.id,
            user_id=self.invitee.id,
            role="ATTENDEE",
            attendance_status="INVITED",
        ))
        # This legacy-style task must not grant an unrelated user access.
        self.outsider_task = PortalMeetingTask(
            meeting_id=self.meeting.id,
            title="Private follow-up",
            assignee_user_id=self.outsider.id,
            created_by_user_id=self.organizer.id,
        )
        db.session.add(self.outsider_task)
        db.session.commit()

    def _login(self, client, user_id):
        g.pop("_login_user", None)
        with client.session_transaction() as session:
            session.clear()
            session["_user_id"] = str(user_id)
            session["_fresh"] = True

    def test_only_organizer_and_invitee_can_view_the_meeting(self):
        client = self.app.test_client()

        self._login(client, self.organizer.id)
        self.assertEqual(client.get(f"/portal/meetings/{self.meeting.id}").status_code, 200)

        self._login(client, self.invitee.id)
        invitation_view = client.get(f"/portal/meetings/{self.meeting.id}")
        self.assertEqual(invitation_view.status_code, 200)
        self.assertIn("الداعي: Organizer".encode("utf-8"), invitation_view.data)
        self.assertIn(self.meeting.title.encode("utf-8"), client.get("/portal/").data)

        for user in (self.outsider, self.meetings_admin):
            with self.subTest(user=user.email):
                self._login(client, user.id)
                self.assertEqual(client.get(f"/portal/meetings/{self.meeting.id}").status_code, 403)
                dashboard = client.get("/portal/meetings")
                self.assertEqual(dashboard.status_code, 200)
                self.assertNotIn(self.meeting.title.encode("utf-8"), dashboard.data)

        for user in (self.admin, self.super_admin):
            with self.subTest(administrator=user.email):
                self._login(client, user.id)
                self.assertEqual(client.get(f"/portal/meetings/{self.meeting.id}").status_code, 200)
                dashboard = client.get("/portal/meetings")
                self.assertEqual(dashboard.status_code, 200)
                self.assertIn(self.meeting.title.encode("utf-8"), dashboard.data)

    def test_tasks_do_not_grant_access_or_notify_uninvited_users(self):
        client = self.app.test_client()

        self._login(client, self.outsider.id)
        self.assertEqual(
            client.post(
                f"/portal/meetings/{self.meeting.id}/tasks/{self.outsider_task.id}/status",
                data={"status": "DONE"},
            ).status_code,
            403,
        )

        self._login(client, self.organizer.id)
        response = client.post(
            f"/portal/meetings/{self.meeting.id}/tasks",
            data={
                "task_title": "Attempted external task",
                "task_assignee_user_id": str(self.outsider.id),
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(PortalMeetingTask.query.filter_by(meeting_id=self.meeting.id).count(), 1)

    def test_only_super_admin_can_delete_a_meeting(self):
        client = self.app.test_client()
        delete_url = f"/portal/meetings/{self.meeting.id}/delete"

        self._login(client, self.admin.id)
        self.assertEqual(client.post(delete_url).status_code, 403)
        self.assertIsNotNone(db.session.get(PortalMeeting, self.meeting.id))

        self._login(client, self.super_admin.id)
        view = client.get(f"/portal/meetings/{self.meeting.id}")
        self.assertIn(delete_url.encode("utf-8"), view.data)
        self.assertEqual(client.post(delete_url).status_code, 302)
        self.assertIsNone(db.session.get(PortalMeeting, self.meeting.id))

    def test_participant_picker_uses_searchable_checkboxes(self):
        client = self.app.test_client()
        self._login(client, self.organizer.id)

        new_form = client.get("/portal/meetings/new")
        self.assertEqual(new_form.status_code, 200)
        self.assertIn(b"newMeetingParticipantPickerSearch", new_form.data)
        self.assertIn(b'type="checkbox" name="participant_ids"', new_form.data)

        edit_view = client.get(f"/portal/meetings/{self.meeting.id}")
        self.assertEqual(edit_view.status_code, 200)
        self.assertIn(b"editMeetingParticipantPickerSearch", edit_view.data)
        self.assertIn(b"data-participant-search", edit_view.data)


if __name__ == "__main__":
    unittest.main()
