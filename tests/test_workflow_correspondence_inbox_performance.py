import unittest
from datetime import datetime, timedelta

from flask import Blueprint, Flask
from sqlalchemy import event

from extensions import db
from models import CorrMovement, InboundMail, OutboundMail, User
from workflow.routes import _correspondence_inbox_tasks


class WorkflowCorrespondenceInboxPerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY="workflow-correspondence-inbox-performance-test",
        )
        db.init_app(cls.app)
        portal = Blueprint("portal", __name__)

        @portal.route("/inbound/<int:inbound_id>", endpoint="inbound_view")
        def inbound_view(inbound_id):
            return str(inbound_id)

        @portal.route("/outbound/<int:outbound_id>", endpoint="outbound_view")
        def outbound_view(outbound_id):
            return str(outbound_id)

        cls.app.register_blueprint(portal, url_prefix="/portal")
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

    def test_inbox_queries_are_bounded_and_only_actor_assignments_are_returned(self):
        actor = User(
            email="corr-inbox-actor@example.test",
            name="Inbox Actor",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        other = User(
            email="corr-inbox-other@example.test",
            name="Other User",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        assigner = User(
            email="corr-inbox-assigner@example.test",
            name="Assignment Author",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        db.session.add_all((actor, other, assigner))
        db.session.flush()

        base_time = datetime(2032, 1, 1, 8, 0)
        visible_ids = []
        for index in range(120):
            assigned_user_id = actor.id if index in {0, 1, 2} else other.id
            inbound = InboundMail(
                ref_no=f"IN-{index}",
                category="GENERAL",
                sender="Sender",
                competence_kind="USER",
                competence_id=assigned_user_id,
                competence_label="Assigned user",
                status="FORWARDED",
                subject=f"Inbound {index}",
                received_date="2032-01-01",
                created_at=base_time + timedelta(minutes=index),
            )
            db.session.add(inbound)
            db.session.flush()
            if assigned_user_id == actor.id:
                visible_ids.append(("IN", inbound.id))
                db.session.add(CorrMovement(
                    inbound_id=inbound.id,
                    actor_user_id=assigner.id,
                    action="FORWARD",
                    note=f"Inbound note {index}",
                    created_at=base_time + timedelta(minutes=index, seconds=1),
                ))

        for index in range(120):
            assigned_user_id = actor.id if index in {0, 1} else other.id
            outbound = OutboundMail(
                ref_no=f"OUT-{index}",
                category="GENERAL",
                recipient="Recipient",
                current_target_kind="USER",
                current_target_id=assigned_user_id,
                current_target_label="Assigned user",
                status="DRAFT",
                subject=f"Outbound {index}",
                sent_date="2032-01-01",
                created_at=base_time + timedelta(minutes=200 + index),
            )
            db.session.add(outbound)
            db.session.flush()
            if assigned_user_id == actor.id:
                visible_ids.append(("OUT", outbound.id))
                db.session.add(CorrMovement(
                    outbound_id=outbound.id,
                    actor_user_id=assigner.id,
                    action="RETURN",
                    note=f"Outbound note {index}",
                    created_at=base_time + timedelta(minutes=200 + index, seconds=1),
                ))
        db.session.commit()
        db.session.expire_all()

        query_count = 0

        def count_queries(conn, cursor, statement, parameters, context, executemany):
            nonlocal query_count
            query_count += 1

        event.listen(db.engine, "before_cursor_execute", count_queries)
        try:
            actor = db.session.get(User, actor.id)
            query_count = 0
            with self.app.test_request_context("/workflow/inbox"):
                tasks = _correspondence_inbox_tasks([actor])
        finally:
            event.remove(db.engine, "before_cursor_execute", count_queries)

        self.assertEqual(
            {(task["kind"], task["item"].id) for task in tasks},
            set(visible_ids),
        )
        self.assertTrue(all(task["assigned_by"].id == assigner.id for task in tasks))
        self.assertTrue(all(task["note"] for task in tasks))
        self.assertLessEqual(query_count, 25)


if __name__ == "__main__":
    unittest.main()
