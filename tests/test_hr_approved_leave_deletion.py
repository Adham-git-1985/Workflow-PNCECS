import tempfile
import unittest
from pathlib import Path

from flask import Flask, g
from flask_login import LoginManager

from extensions import db
from models import (
    AuditLog,
    HRLeaveAttachment,
    HRLeaveRequest,
    HRLeaveType,
    HRRequestApprovalStep,
    HRRequestObserver,
    Notification,
    NotificationEmailDelivery,
    User,
)
from portal import portal_bp
from portal.perm_defs import ALL_KEYS


class ApprovedLeaveDeletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.instance_dir = tempfile.TemporaryDirectory()
        project_root = Path(__file__).resolve().parents[1]
        cls.app = Flask(
            __name__,
            template_folder=str(project_root / "templates"),
            static_folder=str(project_root / "static"),
            instance_path=cls.instance_dir.name,
        )
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="approved-leave-deletion-test",
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
        cls.instance_dir.cleanup()

    def setUp(self):
        db.session.remove()
        db.drop_all()
        db.create_all()

        self.employee = User(email="employee@example.test", name="Employee", password_hash="x", role="employee")
        self.regular_user = User(email="hr@example.test", name="HR User", password_hash="x", role="HR")
        self.admin = User(email="admin@example.test", name="Admin", password_hash="x", role="ADMIN")
        self.super_admin = User(email="super@example.test", name="Super Admin", password_hash="x", role="SUPER_ADMIN")
        self.leave_type = HRLeaveType(code="ANNUAL", name_ar="سنوية", is_active=True)
        db.session.add_all((
            self.employee,
            self.regular_user,
            self.admin,
            self.super_admin,
            self.leave_type,
        ))
        db.session.commit()

    def _login(self, client, user_id):
        g.pop("_login_user", None)
        with client.session_transaction() as session:
            session.clear()
            session["_user_id"] = str(user_id)
            session["_fresh"] = True

    def _approved_leave(self):
        row = HRLeaveRequest(
            user_id=self.employee.id,
            leave_type_id=self.leave_type.id,
            start_date="2026-09-01",
            end_date="2026-09-02",
            days=2,
            status="APPROVED",
        )
        db.session.add(row)
        db.session.flush()
        db.session.add_all((
            HRLeaveAttachment(
                request_id=row.id,
                original_name="medical-report.pdf",
                stored_name="medical-report.pdf",
                uploaded_by_id=self.employee.id,
            ),
            HRRequestApprovalStep(
                request_kind="LEAVE",
                request_id=row.id,
                step_order=1,
                stage_code="DIRECT_MANAGER",
                status="APPROVED",
            ),
            HRRequestObserver(
                request_kind="LEAVE",
                request_id=row.id,
                user_id=self.regular_user.id,
                observer_scope="HR",
            ),
            Notification(
                user_id=self.regular_user.id,
                message="تم اعتماد الإجازة.",
                link_url=f"/portal/hr/approvals/leaves/{row.id}",
            ),
        ))
        attachment_dir = Path(self.app.instance_path) / "uploads" / "leaves" / str(row.id)
        attachment_dir.mkdir(parents=True, exist_ok=True)
        (attachment_dir / "medical-report.pdf").write_bytes(b"test attachment")
        db.session.commit()
        return row.id, attachment_dir

    def test_permission_is_listed_in_the_portal_catalog(self):
        self.assertIn("HR_LEAVE_APPROVED_DELETE", ALL_KEYS)

    def test_user_without_the_permission_cannot_delete_an_approved_leave(self):
        leave_id, _ = self._approved_leave()
        client = self.app.test_client()
        self._login(client, self.regular_user.id)

        response = client.post(f"/portal/hr/approvals/leaves/{leave_id}/delete")

        self.assertEqual(response.status_code, 403)
        self.assertIsNotNone(db.session.get(HRLeaveRequest, leave_id))

    def test_admin_and_super_admin_can_permanently_delete_approved_leave(self):
        for actor in (self.admin, self.super_admin):
            with self.subTest(role=actor.role):
                leave_id, attachment_dir = self._approved_leave()
                client = self.app.test_client()
                self._login(client, actor.id)

                response = client.post(f"/portal/hr/approvals/leaves/{leave_id}/delete")

                self.assertEqual(response.status_code, 302)
                self.assertIsNone(db.session.get(HRLeaveRequest, leave_id))
                self.assertEqual(HRLeaveAttachment.query.filter_by(request_id=leave_id).count(), 0)
                self.assertEqual(
                    HRRequestApprovalStep.query.filter_by(
                        request_kind="LEAVE",
                        request_id=leave_id,
                    ).count(),
                    0,
                )
                self.assertEqual(
                    HRRequestObserver.query.filter_by(
                        request_kind="LEAVE",
                        request_id=leave_id,
                    ).count(),
                    0,
                )
                self.assertEqual(
                    Notification.query.filter_by(
                        link_url=f"/portal/hr/approvals/leaves/{leave_id}",
                    ).count(),
                    0,
                )
                self.assertEqual(NotificationEmailDelivery.query.count(), 0)
                self.assertFalse(attachment_dir.exists())
                self.assertIsNotNone(AuditLog.query.filter_by(
                    action="HR_LEAVE_APPROVED_DELETE",
                    target_type="LEAVE_REQUEST",
                    target_id=leave_id,
                ).first())


if __name__ == "__main__":
    unittest.main()
