import tempfile
import unittest
from unittest.mock import patch

from flask import Flask
from flask_login import LoginManager, login_user, logout_user

from extensions import db
from models import (
    MessageRecipient,
    Notification,
    Role,
    RolePermission,
    SystemSetting,
    TransportPermit,
    User,
    UserPermission,
)
from portal.transport import (
    _can_process_movement,
    _can_read_movement_requests,
    _movement_recipient_ids,
    _notify_next_movement_stage,
)


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

        login_manager = LoginManager()
        login_manager.init_app(cls.app)

        @login_manager.user_loader
        def load_user(user_id):
            try:
                return db.session.get(User, int(user_id))
            except (TypeError, ValueError):
                return None

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
        self.transport_alternate = self._user("transport-alternate@example.test")
        self.administrative_manager = self._user("admin@example.test")
        self.other_approver = self._user("other@example.test")
        self.global_admin = self._user("global-admin@example.test", role="ADMIN")
        db.session.add_all([
            self.requester,
            self.direct_manager,
            self.transport_manager,
            self.transport_alternate,
            self.administrative_manager,
            self.other_approver,
            self.global_admin,
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
            UserPermission(
                user_id=self.transport_alternate.id,
                key="TRANSPORT_DIRECTOR_APPROVE",
                is_allowed=True,
            ),
        ])
        db.session.commit()

    @staticmethod
    def _user(email: str, role: str = "EMPLOYEE") -> User:
        return User(
            email=email,
            name=email,
            password_hash="not-used-in-test",
            role=role,
        )

    @staticmethod
    def _set_approver(setting_key: str, user_id: int) -> None:
        db.session.add(SystemSetting(key=setting_key, value=str(user_id)))
        db.session.commit()

    def test_recipients_follow_the_configured_approval_path(self):
        self.assertEqual(_movement_recipient_ids(self.permit), [self.direct_manager.id])

        self.permit.approval_stage = "TRANSPORT"
        self._set_approver("TRANSPORT_MANAGER_USER_ID", self.transport_manager.id)
        self._set_approver("TRANSPORT_DIRECTOR_USER_ID", self.transport_alternate.id)
        self.assertEqual(_movement_recipient_ids(self.permit), [self.transport_manager.id])

        self.permit.approval_stage = "ADMIN"
        self._set_approver("TRANSPORT_ADMIN_USER_ID", self.administrative_manager.id)
        self.assertEqual(_movement_recipient_ids(self.permit), [self.administrative_manager.id])

    def test_unconfigured_stages_route_to_explicit_permission_holders(self):
        self.permit.approval_stage = "TRANSPORT"
        self.assertEqual(_movement_recipient_ids(self.permit), [self.other_approver.id])

        self.permit.approval_stage = "ADMIN"
        self.assertEqual(_movement_recipient_ids(self.permit), [self.other_approver.id])

    def test_alternate_receives_and_can_process_when_no_primary_is_assigned(self):
        UserPermission.query.filter_by(
            user_id=self.other_approver.id,
            key="TRANSPORT_MANAGER_APPROVE",
        ).delete()
        db.session.commit()
        self.permit.approval_stage = "TRANSPORT"

        self.assertEqual(_movement_recipient_ids(self.permit), [self.transport_alternate.id])
        with self.app.test_request_context():
            login_user(self.transport_alternate)
            self.assertTrue(_can_read_movement_requests())
            self.assertTrue(_can_process_movement(self.permit))
            logout_user()

    def test_role_permission_assignment_routes_to_users_with_localized_role_name(self):
        UserPermission.query.filter_by(
            user_id=self.other_approver.id,
            key="TRANSPORT_MANAGER_APPROVE",
        ).delete()
        role_user = self._user("role-transport@example.test", role="مسؤول النقل")
        db.session.add_all([
            Role(code="transport_officer", name_ar="مسؤول النقل"),
            RolePermission(
                role="transport_officer",
                permission="TRANSPORT_MANAGER_APPROVE",
            ),
            role_user,
        ])
        db.session.commit()
        self.permit.approval_stage = "TRANSPORT"

        self.assertEqual(_movement_recipient_ids(self.permit), [role_user.id])

    def test_transport_stage_creates_notification_and_internal_message_for_responsible_user(self):
        self.permit.approval_stage = "TRANSPORT"
        with self.app.test_request_context(), patch(
            "portal.transport.url_for",
            return_value=f"/portal/transport/permits/{self.permit.id}",
        ):
            login_user(self.requester)
            _notify_next_movement_stage(self.permit)
            db.session.commit()
            logout_user()

        self.assertEqual(
            [item.user_id for item in Notification.query.order_by(Notification.id).all()],
            [self.other_approver.id],
        )
        self.assertEqual(
            [item.recipient_user_id for item in MessageRecipient.query.order_by(MessageRecipient.id).all()],
            [self.other_approver.id],
        )

    def test_requester_cannot_process_own_permit(self):
        db.session.add(UserPermission(
            user_id=self.requester.id,
            key="TRANSPORT_APPROVE",
            is_allowed=True,
        ))
        db.session.commit()

        with self.app.test_request_context():
            login_user(self.requester)
            self.assertFalse(_can_process_movement(self.permit))
            logout_user()
