import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask, g
from flask_login import LoginManager

from extensions import db
from models import (
    AuditLog,
    CorrAttachment,
    CorrMovement,
    InboundMail,
    OutboundMail,
    User,
    WorkflowRequest,
)
from portal import portal_bp


class CorrespondenceDeletionRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.app = Flask(__name__, instance_path=cls.temp_dir.name)
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="correspondence-deletion-test",
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

        cls.app.register_blueprint(portal_bp)
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

        self.super_admin = User(
            email="super-admin@example.test",
            name="Super Admin",
            password_hash="not-used-in-test",
            role="SUPER_ADMIN",
        )
        self.owner = User(
            email="correspondence-owner@example.test",
            name="Correspondence Owner",
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        db.session.add_all((self.super_admin, self.owner))
        db.session.commit()

    def _login(self, client, user_id):
        g.pop("_login_user", None)
        with client.session_transaction() as session:
            session.clear()
            session["_user_id"] = str(user_id)
            session["_fresh"] = True

    def _attachment_path(self, stored_name):
        path = (
            Path(self.temp_dir.name)
            / "uploads"
            / "correspondence"
            / stored_name
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test attachment")
        return path

    def _inbound_with_procedure(self):
        item = InboundMail(
            ref_no="IN-DELETE-TEST",
            category="GENERAL",
            sender="Test Sender",
            subject="Secret inbound deletion test",
            received_date="2026-08-26",
            confidentiality="SECRET",
            status="IN_PROGRESS",
            created_by_id=self.owner.id,
        )
        db.session.add(item)
        db.session.flush()

        movement = CorrMovement(
            inbound_id=item.id,
            actor_user_id=self.owner.id,
            action="INTERNAL_NOTE",
            from_status="IN_PROGRESS",
            to_status="IN_PROGRESS",
            note="Inbound procedure",
        )
        db.session.add(movement)
        db.session.flush()

        attachment = CorrAttachment(
            inbound_id=item.id,
            movement_id=movement.id,
            original_name="inbound-note.txt",
            stored_name="inbound-note.txt",
            uploaded_by_id=self.owner.id,
        )
        db.session.add(attachment)
        db.session.commit()
        return item.id, movement.id, attachment.id, self._attachment_path(attachment.stored_name)

    def _outbound_with_procedure(self):
        item = OutboundMail(
            ref_no="OUT-DELETE-TEST",
            category="GENERAL",
            recipient="Test Recipient",
            subject="Outbound deletion test",
            sent_date="2026-08-26",
            status="APPROVED",
            created_by_id=self.owner.id,
        )
        db.session.add(item)
        db.session.flush()

        movement = CorrMovement(
            outbound_id=item.id,
            actor_user_id=self.owner.id,
            action="APPROVE",
            from_status="WAITING_APPROVAL",
            to_status="APPROVED",
            note="Outbound procedure",
        )
        db.session.add(movement)
        db.session.flush()

        attachment = CorrAttachment(
            outbound_id=item.id,
            movement_id=movement.id,
            original_name="outbound-approval.txt",
            stored_name="outbound-approval.txt",
            uploaded_by_id=self.owner.id,
        )
        db.session.add(attachment)
        db.session.commit()
        return item.id, movement.id, attachment.id, self._attachment_path(attachment.stored_name)

    def test_super_admin_can_delete_inbound_with_its_procedures(self):
        item_id, movement_id, attachment_id, attachment_path = self._inbound_with_procedure()
        official_reply = OutboundMail(
            ref_no="OUT-LINKED-REPLY",
            category="GENERAL",
            recipient="Reply Recipient",
            subject="Official reply",
            sent_date="2026-08-26",
            source_inbound_id=item_id,
            created_by_id=self.owner.id,
        )
        workflow_request = WorkflowRequest(
            title="Inbound workflow",
            status="IN_PROGRESS",
            requester_id=self.owner.id,
            source_corr_kind="IN",
            source_corr_id=item_id,
        )
        db.session.add_all((official_reply, workflow_request))
        db.session.commit()
        official_reply_id = official_reply.id
        workflow_request_id = workflow_request.id

        with self.app.test_client() as client:
            self._login(client, self.super_admin.id)
            response = client.post(f"/portal/corr/inbound/{item_id}/delete")

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(db.session.get(InboundMail, item_id))
        self.assertIsNone(db.session.get(CorrMovement, movement_id))
        self.assertIsNone(db.session.get(CorrAttachment, attachment_id))
        self.assertFalse(attachment_path.exists())
        preserved_reply = db.session.get(OutboundMail, official_reply_id)
        self.assertIsNotNone(preserved_reply)
        self.assertIsNone(preserved_reply.source_inbound_id)
        preserved_workflow = db.session.get(WorkflowRequest, workflow_request_id)
        self.assertIsNotNone(preserved_workflow)
        self.assertIsNone(preserved_workflow.source_corr_kind)
        self.assertIsNone(preserved_workflow.source_corr_id)
        self.assertIsNotNone(AuditLog.query.filter_by(
            action="CORR_IN_DELETE",
            user_id=self.super_admin.id,
            target_id=item_id,
        ).first())

    def test_super_admin_can_delete_outbound_with_its_procedures(self):
        item_id, movement_id, attachment_id, attachment_path = self._outbound_with_procedure()
        workflow_request = WorkflowRequest(
            title="Outbound workflow",
            status="IN_PROGRESS",
            requester_id=self.owner.id,
            source_corr_kind="OUT",
            source_corr_id=item_id,
        )
        db.session.add(workflow_request)
        db.session.commit()
        workflow_request_id = workflow_request.id

        with self.app.test_client() as client:
            self._login(client, self.super_admin.id)
            response = client.post(f"/portal/corr/outbound/{item_id}/delete")

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(db.session.get(OutboundMail, item_id))
        self.assertIsNone(db.session.get(CorrMovement, movement_id))
        self.assertIsNone(db.session.get(CorrAttachment, attachment_id))
        self.assertFalse(attachment_path.exists())
        preserved_workflow = db.session.get(WorkflowRequest, workflow_request_id)
        self.assertIsNotNone(preserved_workflow)
        self.assertIsNone(preserved_workflow.source_corr_kind)
        self.assertIsNone(preserved_workflow.source_corr_id)
        self.assertIsNotNone(AuditLog.query.filter_by(
            action="CORR_OUT_DELETE",
            user_id=self.super_admin.id,
            target_id=item_id,
        ).first())

    def test_super_admin_can_delete_one_procedure_and_its_attachments(self):
        item_id, movement_id, attachment_id, attachment_path = self._inbound_with_procedure()

        with self.app.test_client() as client:
            self._login(client, self.super_admin.id)
            response = client.post(
                f"/portal/corr/IN/{item_id}/procedure/{movement_id}/delete"
            )

        self.assertEqual(response.status_code, 302)
        item = db.session.get(InboundMail, item_id)
        self.assertIsNotNone(item)
        self.assertEqual(item.status, "IN_PROGRESS")
        self.assertIsNone(db.session.get(CorrMovement, movement_id))
        self.assertIsNone(db.session.get(CorrAttachment, attachment_id))
        self.assertFalse(attachment_path.exists())
        self.assertIsNotNone(AuditLog.query.filter_by(
            action="CORR_PROCEDURE_DELETE",
            user_id=self.super_admin.id,
            target_id=item_id,
        ).first())

    def test_non_super_admin_cannot_delete_a_procedure(self):
        item_id, movement_id, attachment_id, attachment_path = self._outbound_with_procedure()

        with self.app.test_client() as client:
            self._login(client, self.owner.id)
            response = client.post(
                f"/portal/corr/OUT/{item_id}/procedure/{movement_id}/delete"
            )

        self.assertEqual(response.status_code, 403)
        self.assertIsNotNone(db.session.get(OutboundMail, item_id))
        self.assertIsNotNone(db.session.get(CorrMovement, movement_id))
        self.assertIsNotNone(db.session.get(CorrAttachment, attachment_id))
        self.assertTrue(attachment_path.exists())

    def test_super_admin_gets_delete_control_on_inbound_list(self):
        with self.app.test_client() as client:
            self._login(client, self.super_admin.id)
            with patch("portal.routes.render_template", return_value="rendered") as render:
                response = client.get("/portal/corr/inbound")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(render.call_args.args[0], "portal/corr/inbound_list.html")
        self.assertTrue(render.call_args.kwargs["can_delete"])


if __name__ == "__main__":
    unittest.main()
