import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from flask import Flask, g
from sqlalchemy import event, inspect

from extensions import db
from models import (
    HRAttendanceSchedulePlan,
    HRAttendanceSpecialCase,
    HRRequestApprovalStep,
    Role,
    RolePermission,
    User,
    UserPermission,
)
from portal.routes import (
    _attendance_approval_pending_count,
    _attendance_edit_hr_approver_user_ids,
    _current_user_approvable_request_ids,
    _portal_flags,
)
from services.hr_request_workflow import (
    KIND_PERMISSION,
    request_ids_user_can_act_on,
    request_ids_user_participated_in,
)


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

    def test_final_permission_result_is_cached_per_request(self):
        user = User(
            email="cached-result@example.test",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        db.session.add(user)
        db.session.commit()
        db.session.add(UserPermission(
            user_id=user.id,
            key="PERFORMANCE_RESULT_READ",
            is_allowed=True,
        ))
        db.session.commit()

        with self.app.test_request_context("/"):
            request_user = db.session.get(User, user.id)
            self.assertTrue(request_user.has_perm("PERFORMANCE_RESULT_READ"))
            self.assertTrue(request_user.has_perm("PERFORMANCE_RESULT_READ"))
            self.assertEqual(
                g._permission_result_cache[(user.id, "PERFORMANCE_RESULT_READ")],
                True,
            )

    def test_role_permission_result_is_cached_per_request(self):
        user = User(
            email="cached-role-result@example.test",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        db.session.add_all((
            user,
            RolePermission(role="EMPLOYEE", permission="PERFORMANCE_ROLE_CHECK"),
        ))
        db.session.commit()
        role_permission_queries = 0

        def count_role_permission_queries(conn, cursor, statement, parameters, context, executemany):
            nonlocal role_permission_queries
            if "role_permission" in statement.lower():
                role_permission_queries += 1

        event.listen(db.engine, "before_cursor_execute", count_role_permission_queries)
        try:
            with self.app.test_request_context("/"):
                request_user = db.session.get(User, user.id)
                self.assertTrue(request_user.has_role_perm("PERFORMANCE_ROLE_CHECK"))
                self.assertTrue(request_user.has_role_perm("PERFORMANCE_ROLE_CHECK"))
        finally:
            event.remove(db.engine, "before_cursor_execute", count_role_permission_queries)

        self.assertEqual(role_permission_queries, 1)

    def test_role_label_resolution_is_loaded_once_per_request(self):
        user = User(
            email="cached-role-label@example.test",
            password_hash="not-used-in-test",
            role="General Secretary",
        )
        db.session.add_all((
            user,
            Role(
                code="GENERAL_SECRETARY",
                name_en="General Secretary",
                name_ar="الأمين العام",
            ),
        ))
        db.session.commit()

        role_queries = 0

        def count_role_queries(conn, cursor, statement, parameters, context, executemany):
            nonlocal role_queries
            if "from roles" in statement.lower():
                role_queries += 1

        event.listen(db.engine, "before_cursor_execute", count_role_queries)
        try:
            with self.app.test_request_context("/"):
                request_user = db.session.get(User, user.id)
                self.assertTrue(request_user.has_role("GENERAL_SECRETARY"))
                self.assertTrue(request_user.has_role("SECRETARY_GENERAL"))
                self.assertTrue(request_user.has_role("الأمين العام"))
        finally:
            event.remove(db.engine, "before_cursor_execute", count_role_queries)

        self.assertEqual(role_queries, 1)

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

    def test_portal_flags_are_cached_per_request(self):
        user = SimpleNamespace(id=42)
        user.has_perm = Mock(return_value=False)
        with self.app.test_request_context("/"):
            with patch("portal.routes.current_user", user), patch(
                "portal.routes._current_user_approvable_request_ids",
                return_value=[],
            ):
                first = _portal_flags()
                first_call_count = user.has_perm.call_count
                second = _portal_flags()

        self.assertIs(first, second)
        self.assertGreater(first_call_count, 0)
        self.assertEqual(user.has_perm.call_count, first_call_count)

    def test_hr_attendance_badge_counts_assigned_rows_without_loading_the_inbox(self):
        user = User(
            email="attendance-badge@example.test",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        db.session.add(user)
        db.session.flush()
        db.session.add_all((
            UserPermission(
                user_id=user.id,
                key="HR_ATTENDANCE_EDIT_APPROVE",
                is_allowed=True,
            ),
            HRAttendanceSchedulePlan(
                user_id=user.id,
                manager_user_id=user.id,
                period_start="2032-01-04",
                period_end="2032-01-10",
                version_no=1,
                status="SUBMITTED",
                request_type="CHANGE_REQUEST",
            ),
            HRAttendanceSpecialCase(
                user_id=user.id,
                day="2032-01-04",
                kind="MANUAL_ATTENDANCE",
                approval_status="PENDING",
                applied=False,
            ),
        ))
        db.session.commit()

        with self.app.test_request_context("/"):
            request_user = db.session.get(User, user.id)
            with patch(
                "portal.routes._attendance_approval_inbox_rows",
                side_effect=AssertionError("the badge must not hydrate the inbox"),
            ), patch(
                "portal.routes._attendance_edit_hr_approver_user_ids",
                return_value=[user.id],
            ):
                self.assertEqual(_attendance_approval_pending_count(request_user), 2)

    def test_manual_attendance_reviewer_ids_are_cached_per_request(self):
        with self.app.test_request_context("/"):
            with patch(
                "portal.routes.attendance_delay_hr_affairs_manager_user_ids",
                return_value=[41],
            ) as resolve_hr_manager:
                self.assertEqual(_attendance_edit_hr_approver_user_ids(), [41])
                self.assertEqual(_attendance_edit_hr_approver_user_ids(), [41])

        resolve_hr_manager.assert_called_once_with()

    def test_attendance_approval_inbox_indexes_exist_on_fresh_schema(self):
        inspector = inspect(db.engine)
        schedule_indexes = {
            index["name"]
            for index in inspector.get_indexes("hr_attendance_schedule_plan")
        }
        special_case_indexes = {
            index["name"]
            for index in inspector.get_indexes("hr_att_special_case")
        }

        self.assertIn(
            "ix_hr_att_schedule_request_status_updated",
            schedule_indexes,
        )
        self.assertIn(
            "ix_hr_att_special_kind_approval_created",
            special_case_indexes,
        )

    def test_approval_id_lookup_avoids_joined_user_relationships(self):
        user = User(
            email="approval-scalar@example.test",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        db.session.add(user)
        db.session.commit()
        db.session.add(HRRequestApprovalStep(
            request_kind=KIND_PERMISSION,
            request_id=901,
            step_order=1,
            stage_code="HR",
            approver_scope="USER",
            approver_user_id=user.id,
            status="PENDING",
        ))
        db.session.commit()
        step_queries = []

        def capture_step_query(conn, cursor, statement, parameters, context, executemany):
            if "from hr_request_approval_step" in statement.lower():
                step_queries.append(" ".join(statement.lower().split()))

        event.listen(db.engine, "before_cursor_execute", capture_step_query)
        try:
            self.assertEqual(request_ids_user_can_act_on(user, KIND_PERMISSION), [901])
        finally:
            event.remove(db.engine, "before_cursor_execute", capture_step_query)

        self.assertEqual(len(step_queries), 1)
        self.assertNotIn("join users", step_queries[0])

    def test_participated_id_lookup_avoids_joined_user_relationships(self):
        user = User(
            email="history-scalar@example.test",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        db.session.add(user)
        db.session.commit()
        db.session.add(HRRequestApprovalStep(
            request_kind=KIND_PERMISSION,
            request_id=902,
            step_order=1,
            stage_code="HR",
            approver_scope="USER",
            approver_user_id=user.id,
            decided_by_id=user.id,
            status="APPROVED",
        ))
        db.session.commit()
        step_queries = []

        def capture_step_query(conn, cursor, statement, parameters, context, executemany):
            if "from hr_request_approval_step" in statement.lower():
                step_queries.append(" ".join(statement.lower().split()))

        event.listen(db.engine, "before_cursor_execute", capture_step_query)
        try:
            self.assertEqual(request_ids_user_participated_in(user, KIND_PERMISSION), [902])
        finally:
            event.remove(db.engine, "before_cursor_execute", capture_step_query)

        self.assertEqual(len(step_queries), 1)
        self.assertNotIn("join users", step_queries[0])


if __name__ == "__main__":
    unittest.main()
