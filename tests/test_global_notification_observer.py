import unittest

from flask import Flask
from sqlalchemy.exc import IntegrityError

from extensions import db
from models import Notification, Role, RolePermission, User, UserPermission
from portal.perm_defs import ALL_KEYS as PORTAL_ALL_KEYS, PERMS as PORTAL_PERMS


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

    def test_general_secretary_gets_one_copy_for_a_multi_recipient_event_and_retry(self):
        role = Role(code="GENERAL_SECRETARY", name_ar="الأمين العام", is_active=True)
        db.session.add(role)
        secretary = self._user("general-secretary@example.test", role=role.code)
        first_recipient = self._user("batch-first@example.test")
        second_recipient = self._user("batch-second@example.test")
        retry_recipient = self._user("batch-retry@example.test")
        db.session.add(RolePermission(
            role=role.code,
            permission=OBSERVER_PERMISSION,
        ))
        db.session.commit()

        event_key = "workflow-secretary-batch-1"
        db.session.add_all((
            Notification(
                user_id=first_recipient.id,
                message="Batch update",
                type="INFO",
                source="workflow",
                event_key=event_key,
            ),
            Notification(
                user_id=second_recipient.id,
                message="Batch update",
                type="INFO",
                source="workflow",
                event_key=event_key,
            ),
        ))
        db.session.commit()

        # A retry/remainder of the same event must not create a second copy.
        db.session.add(Notification(
            user_id=retry_recipient.id,
            message="Batch update",
            type="INFO",
            source="workflow",
            event_key=event_key,
        ))
        db.session.commit()

        self.assertEqual(Notification.query.filter_by(
            user_id=secretary.id,
            source="workflow",
            event_key=event_key,
            is_mirror=False,
        ).count(), 1)

    def test_leave_request_notifications_are_not_copied_to_global_observers(self):
        observer = self._user("leave-observer@example.test")
        recipient = self._user("leave-recipient@example.test")
        db.session.add(UserPermission(
            user_id=observer.id,
            key=OBSERVER_PERMISSION,
            is_allowed=True,
        ))
        db.session.commit()

        db.session.add(Notification(
            user_id=recipient.id,
            message="طلب إجازة بانتظار الاعتماد",
            type="HR_APPROVAL",
            source="portal",
            link_url="/portal/hr/approvals/leaves/99",
        ))
        db.session.commit()

        self.assertEqual(Notification.query.filter_by(
            user_id=observer.id,
            is_mirror=False,
        ).count(), 0)

    def test_departure_request_notifications_are_not_copied_to_global_observers(self):
        observer = self._user("departure-observer@example.test")
        recipient = self._user("departure-recipient@example.test")
        db.session.add(UserPermission(
            user_id=observer.id,
            key=OBSERVER_PERMISSION,
            is_allowed=True,
        ))
        db.session.commit()

        db.session.add(Notification(
            user_id=recipient.id,
            message="طلب مغادرة بانتظار الاعتماد",
            type="HR_APPROVAL",
            source="portal",
            link_url="/portal/hr/approvals/permissions/99",
        ))
        db.session.commit()

        self.assertEqual(Notification.query.filter_by(
            user_id=observer.id,
            is_mirror=False,
        ).count(), 0)

    def test_leave_deduction_receipt_is_employee_only_and_idempotent(self):
        observer = self._user("deduction-observer@example.test")
        employee = self._user("deduction-employee@example.test")
        db.session.add(UserPermission(
            user_id=observer.id,
            key=OBSERVER_PERMISSION,
            is_allowed=True,
        ))
        db.session.commit()

        event_key = f"hr-deduction:final:42:{employee.id}"
        db.session.add(Notification(
            user_id=employee.id,
            message="تم اعتماد خصم الإجازة",
            type="HR_DEDUCTION_FINAL",
            source="portal",
            link_url=f"/portal/hr/me/balances-deductions?year=2026#myDeduction42",
            email_delivery_mode="HR_DEDUCTION",
            event_key=event_key,
        ))
        db.session.commit()

        self.assertEqual(Notification.query.filter_by(
            user_id=observer.id,
            is_mirror=False,
        ).count(), 0)
        self.assertEqual(Notification.query.filter_by(
            user_id=employee.id,
            event_key=event_key,
            is_mirror=False,
        ).count(), 1)

        db.session.add(Notification(
            user_id=employee.id,
            message="تكرار غير مقصود",
            type="HR_DEDUCTION_FINAL",
            source="portal",
            link_url="/portal/hr/me/balances-deductions?year=2026",
            email_delivery_mode="HR_DEDUCTION",
            event_key=event_key,
        ))
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        self.assertEqual(Notification.query.filter_by(
            user_id=employee.id,
            event_key=event_key,
            is_mirror=False,
        ).count(), 1)

    def test_observer_permission_is_available_in_the_portal_permission_editor(self):
        self.assertIn(OBSERVER_PERMISSION, PORTAL_ALL_KEYS)
        definitions = [perm for group in PORTAL_PERMS.values() for perm in group]
        definition = next(perm for perm in definitions if perm.key == OBSERVER_PERMISSION)
        self.assertEqual(definition.label, "مراقب عام للإشعارات")


if __name__ == "__main__":
    unittest.main()
