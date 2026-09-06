import unittest

from flask import Flask

from extensions import db
from models import Notification, Role, RolePermission, User, UserPermission


OBSERVER_PERMISSION = "NOTIFICATIONS_GLOBAL_OBSERVER"


class GlobalNotificationObserverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY="global-notification-observer-test",
        )
        db.init_app(cls.app)
        cls.context = cls.app.app_context()
        cls.context.push()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.context.pop()

    def setUp(self):
        db.session.remove()
        db.drop_all()
        db.create_all()

    @staticmethod
    def _user(email, role="EMPLOYEE"):
        user = User(email=email, password_hash="not-used-in-test", role=role)
        db.session.add(user)
        db.session.flush()
        return user

    def test_user_permission_receives_one_copy_in_each_matching_centre(self):
        observer = self._user("observer@example.test")
        first_recipient = self._user("first@example.test")
        second_recipient = self._user("second@example.test")
        db.session.add(UserPermission(
            user_id=observer.id,
            key=OBSERVER_PERMISSION,
            is_allowed=True,
        ))
        db.session.commit()

        db.session.add_all((
            Notification(
                user_id=first_recipient.id,
                message="تحديث مسار",
                type="WORKFLOW",
                source="workflow",
                event_key="workflow-event-1",
            ),
            Notification(
                user_id=second_recipient.id,
                message="تحديث مسار",
                type="WORKFLOW",
                source="workflow",
                event_key="workflow-event-1",
            ),
            Notification(
                user_id=first_recipient.id,
                message="تحديث بوابة",
                type="PORTAL",
                source="portal",
                event_key="portal-event-1",
            ),
            Notification(
                user_id=first_recipient.id,
                message="متابعة مرسلة",
                type="WORKFLOW",
                source="workflow",
                event_key="mirror-event-1",
                is_mirror=True,
            ),
        ))
        db.session.commit()

        observer_notifications = Notification.query.filter_by(
            user_id=observer.id,
            is_mirror=False,
        ).all()
        self.assertEqual(len(observer_notifications), 2)
        self.assertEqual(
            {(row.source, row.message) for row in observer_notifications},
            {("workflow", "تحديث مسار"), ("portal", "تحديث بوابة")},
        )

    def test_role_permission_is_explicit_and_does_not_duplicate_direct_delivery(self):
        role = Role(code="GENERAL_SECRETARY", name_ar="الأمين العام", is_active=True)
        db.session.add(role)
        observer = self._user("secretary@example.test", role=role.code)
        normal_recipient = self._user("recipient@example.test")
        db.session.add(RolePermission(
            role=role.code,
            permission=OBSERVER_PERMISSION,
        ))
        db.session.commit()

        db.session.add_all((
            Notification(
                user_id=observer.id,
                message="تم إرساله مباشرة للمراقب",
                type="INFO",
                source="portal",
                event_key="portal-event-2",
            ),
            Notification(
                user_id=normal_recipient.id,
                message="تم إرساله مباشرة للمراقب",
                type="INFO",
                source="portal",
                event_key="portal-event-2",
            ),
        ))
        db.session.commit()

        self.assertEqual(
            Notification.query.filter_by(
                user_id=observer.id,
                source="portal",
                event_key="portal-event-2",
                is_mirror=False,
            ).count(),
            1,
        )


if __name__ == "__main__":
    unittest.main()
