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
    def test_personal_mode_keeps_logged_in_users_portal_access(self):
        delegatee = _PermissionUser(4, permissions={"PORTAL_READ"})
        delegator = _PermissionUser(31)

        @perm_required("PORTAL_READ")
        def protected_view():
            return "allowed"

        with patch("utils.perms.current_user", delegatee), patch(
            "utils.perms.get_effective_user",
            return_value=delegator,
        ), patch("utils.perms.is_delegated_identity_selected", return_value=False):
            self.assertEqual(protected_view(), "allowed")

    def test_selected_delegation_uses_only_the_principal_permissions(self):
        delegatee = _PermissionUser(4, permissions={"PORTAL_READ"})
        delegator = _PermissionUser(31)

        @perm_required("PORTAL_READ")
        def protected_view():
            return "allowed"

        with patch("utils.perms.current_user", delegatee), patch(
            "utils.perms.get_effective_user",
            return_value=delegator,
        ), patch("utils.perms.is_delegated_identity_selected", return_value=True):
            with self.assertRaises(Forbidden):
                protected_view()

    def test_delegator_can_extend_logged_in_users_permissions(self):
        delegatee = _PermissionUser(4)
        delegator = _PermissionUser(31, permissions={"PORTAL_READ"})

        @perm_required("PORTAL_READ")
        def protected_view():
            return "allowed"

        with patch("utils.perms.current_user", delegatee), patch(
            "utils.perms.get_effective_user",
            return_value=delegator,
        ), patch("utils.perms.is_delegated_identity_selected", return_value=True):
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
        ), patch("utils.perms.is_delegated_identity_selected", return_value=True):
            with self.assertRaises(Forbidden):
                protected_view()

    def test_portal_layout_uses_selected_working_identity(self):
        template_path = (
            Path(__file__).resolve().parents[1]
            / "templates"
            / "portal"
            / "layout.html"
        )
        source = template_path.read_text(encoding="utf-8")

        self.assertIn("{% set au = display_user %}", source)
        self.assertIn("{% set display_user = working_user|default(current_user, true) %}", source)


if __name__ == "__main__":
    unittest.main()
