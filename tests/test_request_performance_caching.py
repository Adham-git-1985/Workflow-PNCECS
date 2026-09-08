import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask
from sqlalchemy import event

from extensions import db
from models import RolePermission, User
from portal.routes import _current_user_approvable_request_ids


class RequestPerformanceCachingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY="request-performance-caching-test",
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

    def test_role_permissions_are_loaded_once_per_request(self):
        user = User(
            email="cached-role@example.test",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        db.session.add_all((
            user,
            RolePermission(role="EMPLOYEE", permission="PORTAL_MANAGE"),
        ))
        db.session.commit()
        user_id = user.id
        role_permission_queries = 0

        def count_role_permission_queries(conn, cursor, statement, parameters, context, executemany):
            nonlocal role_permission_queries
            if "role_permission" in statement.lower():
                role_permission_queries += 1

        event.listen(db.engine, "before_cursor_execute", count_role_permission_queries)
        try:
            with self.app.test_request_context("/"):
                request_user = db.session.get(User, user_id)
                self.assertTrue(request_user.has_perm("PORTAL_READ"))
                self.assertTrue(request_user.has_perm("PORTAL_CREATE"))
                self.assertTrue(request_user.has_perm("PORTAL_UPDATE"))
                self.assertTrue(request_user.has_perm("PORTAL_DELETE"))
        finally:
            event.remove(db.engine, "before_cursor_execute", count_role_permission_queries)

        self.assertEqual(role_permission_queries, 1)

    def test_general_secretary_role_aliases_share_permissions(self):
        user = User(
            email="secretary-role-alias@example.test",
            password_hash="not-used-in-test",
            role="General_secretary",
        )
        db.session.add_all((
            user,
            RolePermission(
                role="GENERAL-SECRETARY",
                permission="HR_REQUESTS_APPROVE",
            ),
        ))
        db.session.commit()

        with self.app.test_request_context("/"):
            request_user = db.session.get(User, user.id)
            self.assertTrue(request_user.has_perm("HR_REQUESTS_APPROVE"))
            self.assertTrue(request_user.has_role_perm("hr_requests_approve"))
            self.assertTrue(request_user.has_role("GENERAL_SECRETARY"))
            self.assertTrue(request_user.has_role("SECRETARY_GENERAL"))
            self.assertTrue(request_user.has_role("الأمين العام"))

    def test_current_user_approval_ids_are_cached_per_request(self):
        user = SimpleNamespace(id=42)
        with self.app.test_request_context("/"):
            with patch("portal.routes.current_user", user), patch(
                "portal.routes.request_ids_user_can_act_on",
                return_value=[3, 5],
            ) as find_assignments:
                self.assertEqual(_current_user_approvable_request_ids("LEAVE"), [3, 5])
                self.assertEqual(_current_user_approvable_request_ids("LEAVE"), [3, 5])

        find_assignments.assert_called_once_with(user, "LEAVE")


if __name__ == "__main__":
    unittest.main()
