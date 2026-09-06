import unittest

from flask import Flask

from extensions import db
from models import User


class UsernameAccountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
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
        db.drop_all()
        db.create_all()

    def test_programmatic_account_defaults_username_to_its_email(self):
        user = User(
            email="person@example.test",
            password_hash="unused",
            role="EMPLOYEE",
        )
        db.session.add(user)
        db.session.commit()

        self.assertEqual(user.username, "person@example.test")
        self.assertEqual(user.full_name, "person@example.test")

    def test_account_can_have_username_without_email(self):
        user = User(
            username="staff-204",
            email=None,
            password_hash="unused",
            role="EMPLOYEE",
        )
        db.session.add(user)
        db.session.commit()

        self.assertEqual(user.username, "staff-204")
        self.assertEqual(user.full_name, "staff-204")


if __name__ == "__main__":
    unittest.main()
