import unittest

from flask import Flask

from extensions import db
from models import Committee, CommitteeAssignee, User
from utils.committee_display import build_committee_summaries


class CommitteeDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY="committee-display-test",
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
    def _user(email, name, role="EMPLOYEE"):
        user = User(
            email=email,
            username=email,
            name=name,
            password_hash="test",
            role=role,
        )
        db.session.add(user)
        db.session.flush()
        return user

    def test_summary_names_chair_and_compacts_other_members(self):
        chair = self._user("chair@example.test", "رئيس اللجنة")
        member_one = self._user("one@example.test", "عضو أول")
        member_two = self._user("two@example.test", "عضو ثان")
        role_member = self._user("role@example.test", "عضو بالدور", role="REVIEWER")
        committee = Committee(name_ar="لجنة الاختبار", is_active=True)
        db.session.add(committee)
        db.session.flush()
        db.session.add_all([
            CommitteeAssignee(
                committee_id=committee.id,
                kind="USER",
                user_id=chair.id,
                member_role="CHAIR",
                is_active=True,
            ),
            CommitteeAssignee(
                committee_id=committee.id,
                kind="USER",
                user_id=member_one.id,
                member_role="MEMBER",
                is_active=True,
            ),
            CommitteeAssignee(
                committee_id=committee.id,
                kind="USER",
                user_id=member_two.id,
                member_role="MEMBER",
                is_active=True,
            ),
            CommitteeAssignee(
                committee_id=committee.id,
                kind="ROLE",
                role="reviewer",
                member_role="MEMBER",
                is_active=True,
            ),
        ])
        db.session.commit()

        summary = build_committee_summaries([committee.id])[committee.id]

        self.assertEqual(summary["chair"], "رئيس اللجنة")
        self.assertEqual(summary["member_preview"], ["عضو أول", "عضو ثان"])
        self.assertEqual(summary["member_more_count"], 1)
        self.assertEqual(summary["member_names"], ["عضو أول", "عضو ثان", "عضو بالدور"])
