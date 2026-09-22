import unittest
from types import SimpleNamespace

from utils.delegation_privacy import (
    COMPETENT_AUTHORITY_LABEL,
    audit_display_actor,
    audit_display_note,
    can_view_delegation_details,
    redact_super_admin_references,
)


class _User:
    def __init__(self, user_id, name, role="EMPLOYEE"):
        self.id = user_id
        self.full_name = name
        self.name = name
        self.username = name.lower().replace(" ", ".")
        self.email = f"{self.username}@example.test"
        self.role = role

    def has_role(self, role):
        return self.role == role


class DelegationPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.principal = _User(1, "Principal User")
        self.delegate = _User(2, "Delegate User")
        self.outsider = _User(3, "Other User")
        self.admin = _User(4, "Administrator", role="ADMIN")
        self.log = SimpleNamespace(
            actual_user=self.delegate,
            user=self.delegate,
            actual_user_id=self.delegate.id,
            user_id=self.delegate.id,
            acting_for_user=self.principal,
            on_behalf_of_user=self.principal,
            acting_for_user_id=self.principal.id,
            on_behalf_of_id=self.principal.id,
            note="أُنجز الإجراء بواسطة Delegate User",
        )

    def test_only_the_parties_and_administrators_see_the_actual_executor(self):
        self.assertIs(audit_display_actor(self.log, self.outsider), self.principal)
        self.assertFalse(can_view_delegation_details(self.log, self.outsider))

        self.assertIs(audit_display_actor(self.log, self.principal), self.delegate)
        self.assertTrue(can_view_delegation_details(self.log, self.principal))

        self.assertIs(audit_display_actor(self.log, self.delegate), self.delegate)
        self.assertTrue(can_view_delegation_details(self.log, self.delegate))

        self.assertIs(audit_display_actor(self.log, self.admin), self.delegate)
        self.assertTrue(can_view_delegation_details(self.log, self.admin))

    def test_public_note_does_not_name_the_delegate(self):
        self.assertEqual(
            audit_display_note(self.log, self.outsider),
            "أُنجز الإجراء بواسطة Principal User",
        )
        self.assertIn("Delegate User", audit_display_note(self.log, self.delegate))

    def test_super_admin_title_is_replaced_for_an_ordinary_user(self):
        hidden = redact_super_admin_references(
            "السوبر أدمن / Super Admin / SUPER_ADMIN / Super Administrator",
            self.outsider,
        )
        self.assertEqual(
            hidden,
            " / ".join((COMPETENT_AUTHORITY_LABEL,) * 4),
        )
        self.assertEqual(
            redact_super_admin_references("السوبر أدمن", self.admin),
            "السوبر أدمن",
        )


if __name__ == "__main__":
    unittest.main()
