import unittest

from flask import Flask

from extensions import db
from models import User, UserPermission
from services.correspondence_workflow import (
    filter_correspondence_notification_user_ids,
)


class CorrespondenceNotificationRecipientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY="correspondence-notification-test",
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
    def _user(email):
        user = User(
            email=email,
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        db.session.add(user)
        db.session.flush()
        return user

    def test_normal_correspondence_excludes_users_without_read_access(self):
        unrelated = self._user("unrelated@example.test")
        reader = self._user("reader@example.test")
        creator = self._user("creator@example.test")
        assignee = self._user("assignee@example.test")
        db.session.add(UserPermission(
            user_id=reader.id,
            key="CORR_READ",
            is_allowed=True,
        ))
        db.session.commit()

        recipients = filter_correspondence_notification_user_ids(
            [unrelated.id, reader.id, creator.id, assignee.id],
            confidentiality="NORMAL",
            created_by_user_id=creator.id,
            current_assignee_user_id=assignee.id,
        )

        self.assertEqual(recipients, {reader.id, creator.id, assignee.id})

    def test_secret_correspondence_keeps_explicit_access_without_regular_read(self):
        unrelated = self._user("secret-unrelated@example.test")
        authorized = self._user("secret-authorized@example.test")
        confidential_reader = self._user("confidential-reader@example.test")
        db.session.add_all((
            UserPermission(
                user_id=authorized.id,
                key="CORR_READ",
                is_allowed=False,
            ),
            UserPermission(
                user_id=confidential_reader.id,
                key="CORR_CONFIDENTIAL_READ",
                is_allowed=True,
            ),
        ))
        db.session.commit()

        recipients = filter_correspondence_notification_user_ids(
            [unrelated.id, authorized.id, confidential_reader.id],
            confidentiality="SECRET",
            authorized_user_ids={authorized.id},
        )

        self.assertEqual(recipients, {authorized.id, confidential_reader.id})


if __name__ == "__main__":
    unittest.main()
