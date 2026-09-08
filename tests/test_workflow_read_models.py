import inspect
import unittest
from unittest.mock import patch

from flask import Flask
from sqlalchemy import event, inspect as sa_inspect

from extensions import db
from models import AuditLog, User, WorkflowInstance, WorkflowInstanceStep, WorkflowRequest
from workflow.read_models import load_workflow_access_snapshot
from workflow.routes import _user_can_act_on_step, work_dashboard


MENTION_ACCESS_ACTION = "WORKFLOW_MENTION_ACCESS"
MENTION_ACCESS_REVOKED_ACTION = "WORKFLOW_MENTION_ACCESS_REVOKED"
MENTION_TASK_NOTE = "MENTION_TASK"
RETAINED_FOLLOWER_ACTIONS = (
    "HIERARCHY_BYPASS_FOLLOWER",
    "ASSISTANT_SECRETARY_REDIRECT_FOLLOWER",
)


class WorkflowReadModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY="workflow-read-model-test",
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

    def _snapshot(self, requests, actor):
        return load_workflow_access_snapshot(
            requests,
            [actor],
            mention_access_action=MENTION_ACCESS_ACTION,
            mention_access_revoked_action=MENTION_ACCESS_REVOKED_ACTION,
            retained_follower_actions=RETAINED_FOLLOWER_ACTIONS,
            mention_task_notes=(MENTION_TASK_NOTE,),
            mention_task_prefix=MENTION_TASK_NOTE,
        )

    def _seed_direct_requests(self, count):
        actor = User(
            email="actor@example.test",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        requester = User(
            email="requester@example.test",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        db.session.add_all((actor, requester))
        db.session.flush()

        requests = []
        for index in range(count):
            workflow_request = WorkflowRequest(
                title=f"Request {index}",
                status="IN_PROGRESS",
                requester_id=requester.id,
            )
            db.session.add(workflow_request)
            db.session.flush()
            instance = WorkflowInstance(
                request_id=workflow_request.id,
                current_step_order=1,
                is_completed=False,
            )
            db.session.add(instance)
            db.session.flush()
            db.session.add(WorkflowInstanceStep(
                instance_id=instance.id,
                step_order=1,
                mode="SEQUENTIAL",
                approver_kind="USER",
                approver_user_id=actor.id,
                status="PENDING",
            ))
            requests.append(workflow_request)
        db.session.commit()
        return actor, requests

    def test_snapshot_query_count_does_not_grow_per_request(self):
        actor, requests = self._seed_direct_requests(75)
        actor_id = int(actor.id)
        request_ids = [int(row.id) for row in requests]
        db.session.remove()
        actor = db.session.get(User, actor_id)
        requests = (
            WorkflowRequest.query
            .filter(WorkflowRequest.id.in_(request_ids))
            .order_by(WorkflowRequest.id.asc())
            .all()
        )

        def measured_query_count(selected_requests):
            query_count = 0

            def count_queries(conn, cursor, statement, parameters, context, executemany):
                nonlocal query_count
                query_count += 1

            event.listen(db.engine, "before_cursor_execute", count_queries)
            try:
                snapshot = self._snapshot(selected_requests, actor)
                self.assertTrue(all(snapshot.can_view(actor, row) for row in selected_requests))
            finally:
                event.remove(db.engine, "before_cursor_execute", count_queries)
            return query_count

        one_request_queries = measured_query_count(requests[:1])
        many_request_queries = measured_query_count(requests)

        self.assertLessEqual(many_request_queries, one_request_queries + 1)
        self.assertLessEqual(many_request_queries, 12)

    def test_dashboard_query_count_is_bounded_by_page_not_history(self):
        actor, _requests = self._seed_direct_requests(75)
        actor_id = int(actor.id)
        db.session.remove()
        actor = db.session.get(User, actor_id)
        dashboard_view = inspect.unwrap(work_dashboard)

        def render_and_count(path):
            query_count = 0
            rendered_context = {}

            def count_queries(conn, cursor, statement, parameters, context, executemany):
                nonlocal query_count
                query_count += 1

            def capture_template(template_name, **context):
                rendered_context.update(context)
                return context

            event.listen(db.engine, "before_cursor_execute", count_queries)
            try:
                with self.app.test_request_context(path):
                    with patch("workflow.routes.current_user", actor), patch(
                        "workflow.routes.get_active_delegations",
                        return_value=[],
                    ), patch("workflow.routes.render_template", side_effect=capture_template):
                        dashboard_view()
            finally:
                event.remove(db.engine, "before_cursor_execute", count_queries)
            return query_count, rendered_context

        one_request_queries, one_context = render_and_count(
            "/workflow/work?queue=my_action&q=Request+0"
        )
        many_request_queries, many_context = render_and_count(
            "/workflow/work?queue=my_action"
        )

        self.assertEqual(one_context["total"], 1)
        self.assertEqual(many_context["total"], 75)
        self.assertEqual(len(many_context["rows"]), 50)
        self.assertLessEqual(many_request_queries, one_request_queries + 5)

    def test_revoked_mention_does_not_grant_visibility(self):
        actor = User(
            email="mentioned@example.test",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        requester = User(
            email="owner@example.test",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        approver = User(
            email="approver@example.test",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        db.session.add_all((actor, requester, approver))
        db.session.flush()
        workflow_request = WorkflowRequest(
            title="Mention lifecycle",
            status="IN_PROGRESS",
            requester_id=requester.id,
        )
        db.session.add(workflow_request)
        db.session.flush()
        instance = WorkflowInstance(
            request_id=workflow_request.id,
            current_step_order=1,
            is_completed=False,
        )
        db.session.add(instance)
        db.session.flush()
        db.session.add(WorkflowInstanceStep(
            instance_id=instance.id,
            step_order=1,
            mode="SEQUENTIAL",
            approver_kind="USER",
            approver_user_id=approver.id,
            status="PENDING",
        ))
        db.session.add_all((
            AuditLog(
                request_id=workflow_request.id,
                user_id=requester.id,
                action=MENTION_ACCESS_ACTION,
                target_type="USER",
                target_id=actor.id,
            ),
            AuditLog(
                request_id=workflow_request.id,
                user_id=requester.id,
                action=MENTION_ACCESS_REVOKED_ACTION,
                target_type="USER",
                target_id=actor.id,
            ),
        ))
        db.session.commit()

        snapshot = self._snapshot([workflow_request], actor)

        self.assertNotIn(actor.id, snapshot.active_mentions_by_request.get(workflow_request.id, set()))
        self.assertFalse(snapshot.can_view(actor, workflow_request))

    def test_general_secretary_alias_can_act_on_role_step(self):
        actor = User(
            email="secretary@example.test",
            password_hash="not-used-in-test",
            role="General_secretary",
        )
        requester = User(
            email="secretary-requester@example.test",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        db.session.add_all((actor, requester))
        db.session.flush()
        workflow_request = WorkflowRequest(
            title="Secretary alias request",
            status="IN_PROGRESS",
            requester_id=requester.id,
        )
        db.session.add(workflow_request)
        db.session.flush()
        instance = WorkflowInstance(
            request_id=workflow_request.id,
            current_step_order=1,
            is_completed=False,
        )
        db.session.add(instance)
        db.session.flush()
        step = WorkflowInstanceStep(
            instance_id=instance.id,
            step_order=1,
            mode="SEQUENTIAL",
            approver_kind="ROLE",
            approver_role="SECRETARY_GENERAL",
            status="PENDING",
        )
        db.session.add(step)
        db.session.commit()

        snapshot = self._snapshot([workflow_request], actor)

        self.assertTrue(snapshot.can_act(actor, step))
        self.assertTrue(snapshot.can_view(actor, workflow_request))
        self.assertTrue(_user_can_act_on_step(actor, step))

    def test_high_traffic_indexes_are_declared(self):
        inspector = sa_inspect(db.engine)
        expected_indexes = {
            "workflow_request": {"ix_workflow_request_status_id"},
            "audit_log": {
                "ix_audit_request_created",
                "ix_audit_request_action_target",
            },
            "workflow_instance_steps": {
                "ix_workflow_instance_steps_instance_status_order",
            },
            "workflow_step_tasks": {
                "ix_workflow_step_tasks_instance_step_status_user",
                "ix_workflow_step_tasks_request_status",
            },
            "notification": {
                "ix_notification_poll_unread",
                "ix_notification_poll_latest",
            },
        }
        for table_name, expected_names in expected_indexes.items():
            actual_names = {
                row["name"] for row in inspector.get_indexes(table_name)
            }
            self.assertTrue(expected_names.issubset(actual_names))


if __name__ == "__main__":
    unittest.main()
