import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.exceptions import Forbidden

from utils.perms import perm_required


class _PermissionUser:
    def __init__(self, user_id, permissions=(), roles=()):
        self.id = user_id
        self.is_authenticated = True
        self._permissions = set(permissions)
        self._roles = set(roles)

    def has_perm(self, key):
        return key in self._permissions

    def has_role(self, role):
        return role in self._roles


class DelegatedPermissionTests(unittest.TestCase):
    def test_delegation_does_not_remove_logged_in_users_portal_access(self):
        delegatee = _PermissionUser(4, permissions={"PORTAL_READ"})
        delegator = _PermissionUser(31)

        @perm_required("PORTAL_READ")
        def protected_view():
            return "allowed"

        with patch("utils.perms.current_user", delegatee), patch(
            "utils.perms.get_effective_user",
            return_value=delegator,
        ):
            self.assertEqual(protected_view(), "allowed")

    def test_delegator_can_extend_logged_in_users_permissions(self):
        delegatee = _PermissionUser(4)
        delegator = _PermissionUser(31, permissions={"PORTAL_READ"})

        @perm_required("PORTAL_READ")
        def protected_view():
            return "allowed"

        with patch("utils.perms.current_user", delegatee), patch(
            "utils.perms.get_effective_user",
            return_value=delegator,
        ):
            self.assertEqual(protected_view(), "allowed")

    def test_permission_is_still_denied_when_neither_identity_has_it(self):
        delegatee = _PermissionUser(4)
        delegator = _PermissionUser(31)

        @perm_required("PORTAL_READ")
        def protected_view():
            return "allowed"

        with patch("utils.perms.current_user", delegatee), patch(
            "utils.perms.get_effective_user",
            return_value=delegator,
        ):
            with self.assertRaises(Forbidden):
                protected_view()

    def test_portal_layout_keeps_logged_in_user_as_permission_subject(self):
        template_path = (
            Path(__file__).resolve().parents[1]
            / "templates"
            / "portal"
            / "layout.html"
        )
        source = template_path.read_text(encoding="utf-8")

        self.assertIn("{% set au = current_user %}", source)
        self.assertNotIn("{% set au = g.effective_user %}", source)


if __name__ == "__main__":
    unittest.main()
