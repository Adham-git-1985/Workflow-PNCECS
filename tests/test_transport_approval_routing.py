import tempfile
import unittest

from flask import Flask

from extensions import db
from models import SystemSetting, TransportPermit, User, UserPermission
from portal.transport import _movement_recipient_ids


class TransportApprovalRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.app = Flask(__name__, instance_path=cls.temp_dir.name)
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="transport-routing-test",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(cls.app)
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

        self.requester = self._user("requester@example.test")
        self.direct_manager = self._user("manager@example.test")
        self.transport_manager = self._user("transport@example.test")
        self.administrative_manager = self._user("admin@example.test")
        self.other_approver = self._user("other@example.test")
        db.session.add_all([
            self.requester,
            self.direct_manager,
            self.transport_manager,
            self.administrative_manager,
            self.other_approver,
        ])
        db.session.flush()

        self.permit = TransportPermit(
            requester_user_id=self.requester.id,
            purpose="Official mission",
            origin_text="Origin",
            dest_text="Destination",
            status="SUBMITTED",
            approval_stage="MANAGER",
            manager_user_id=self.direct_manager.id,
        )
        db.session.add(self.permit)
        db.session.add_all([
            UserPermission(
                user_id=self.other_approver.id,
                key="TRANSPORT_MANAGER_APPROVE",
                is_allowed=True,
            ),
            UserPermission(
                user_id=self.other_approver.id,
                key="TRANSPORT_ADMIN_APPROVE",
                is_allowed=True,
            ),
        ])
        db.session.commit()

    @staticmethod
    def _user(email: str) -> User:
        return User(
            email=email,
            name=email,
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )

    @staticmethod
    def _set_approver(setting_key: str, user_id: int) -> None:
        db.session.add(SystemSetting(key=setting_key, value=str(user_id)))
        db.session.commit()

    def test_recipients_follow_the_configured_approval_path(self):
        self.assertEqual(_movement_recipient_ids(self.permit), [self.direct_manager.id])

        self.permit.approval_stage = "TRANSPORT"
        self._set_approver("TRANSPORT_MANAGER_USER_ID", self.transport_manager.id)
        self.assertEqual(_movement_recipient_ids(self.permit), [self.transport_manager.id])

        self.permit.approval_stage = "ADMIN"
        self._set_approver("TRANSPORT_ADMIN_USER_ID", self.administrative_manager.id)
        self.assertEqual(_movement_recipient_ids(self.permit), [self.administrative_manager.id])

    def test_unconfigured_stages_do_not_broadcast_to_permission_holders(self):
        self.permit.approval_stage = "TRANSPORT"
        self.assertEqual(_movement_recipient_ids(self.permit), [])

        self.permit.approval_stage = "ADMIN"
        self.assertEqual(_movement_recipient_ids(self.permit), [])
