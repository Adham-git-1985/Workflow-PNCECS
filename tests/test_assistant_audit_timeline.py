from datetime import datetime
import unittest
from unittest.mock import patch

from flask import Flask

from assistant.knowledge import collect_knowledge
from assistant.service import answer
from extensions import db
from models import AuditLog, PortalCircular, User, WorkflowRequest


class AssistantAuditTimelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="assistant-audit-timeline-test",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(cls.app)
        cls.context = cls.app.app_context()
        cls.context.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.context.pop()

    def setUp(self):
        db.session.remove()
        db.drop_all()
        db.create_all()
        self.issuer = self._user("issuer@example.test", "مدير التعميم", "SUPER_ADMIN")
        self.employee = self._user("employee@example.test", "موظف المتابعة")

    @staticmethod
    def _user(email, name, role="EMPLOYEE"):
        row = User(email=email, name=name, password_hash="not-used-in-test", role=role)
        db.session.add(row)
        db.session.flush()
        return row

    def test_circular_question_returns_actor_and_chronological_audit_events(self):
        circular = PortalCircular(
            title="تعميم دوام الموظفين",
            body="تفاصيل الدوام الرسمي",
            target_scope="ALL",
            is_active=True,
            created_by_user_id=self.issuer.id,
        )
        db.session.add(circular)
        db.session.flush()
        db.session.add_all([
            AuditLog(
                user_id=self.issuer.id,
                action="PORTAL_CIRCULAR_CREATE",
                target_type="PORTAL_CIRCULAR",
                target_id=circular.id,
                created_at=datetime(2026, 9, 1, 8, 30),
            ),
            AuditLog(
                user_id=self.employee.id,
                action="PORTAL_CIRCULAR_EMAIL_SEND",
                target_type="PORTAL_CIRCULAR",
                target_id=circular.id,
                created_at=datetime(2026, 9, 1, 9, 0),
            ),
        ])
        db.session.commit()

        result = collect_knowledge(self.employee, "من عمل على تعميم دوام الموظفين؟")

        self.assertIn("audit_timeline", result["intents"])
        self.assertIn("سجل التدقيق والخط الزمني", result["reply"])
        self.assertIn("مدير التعميم", result["reply"])
        self.assertIn("موظف المتابعة", result["reply"])
        self.assertIn("01/09/2026", result["reply"])

        with (
            patch("assistant.service.navigation_results", return_value=[]),
            patch("assistant.service._try_local_ai") as local_ai,
            patch("assistant.service._try_external_ai") as external_ai,
        ):
            assistant_result = answer(self.employee, "من عمل على تعميم دوام الموظفين؟")
        self.assertEqual(assistant_result["mode"], "local")
        self.assertIn("مدير التعميم", assistant_result["reply"])
        local_ai.assert_not_called()
        external_ai.assert_not_called()

    def test_circular_issuer_wording_uses_the_audit_timeline(self):
        circular = PortalCircular(
            title="تعميم البرامج والورش التدريبية الافتراضية",
            body="تفاصيل التعميم",
            target_scope="ALL",
            is_active=True,
            created_by_user_id=self.issuer.id,
        )
        db.session.add(circular)
        db.session.flush()
        db.session.add(AuditLog(
            user_id=self.issuer.id,
            action="PORTAL_CIRCULAR_CREATE",
            target_type="PORTAL_CIRCULAR",
            target_id=circular.id,
            created_at=datetime(2026, 9, 3, 8, 30),
        ))
        db.session.commit()

        result = collect_knowledge(
            self.employee,
            "من أصدر التعميم: تعميم البرامج والورش التدريبية الافتراضية؟",
        )

        self.assertIn("audit_timeline", result["intents"])
        self.assertIn("مدير التعميم", result["reply"])

    def test_hidden_circular_audit_is_not_disclosed_to_an_unprivileged_user(self):
        circular = PortalCircular(
            title="تعميم غير ظاهر لا يجب كشفه",
            body="محتوى داخلي",
            target_scope="ALL",
            is_active=False,
            created_by_user_id=self.issuer.id,
        )
        db.session.add(circular)
        db.session.flush()
        db.session.add(AuditLog(
            user_id=self.issuer.id,
            action="PORTAL_CIRCULAR_CREATE",
            target_type="PORTAL_CIRCULAR",
            target_id=circular.id,
        ))
        db.session.commit()

        result = collect_knowledge(self.employee, f"سجل التدقيق للتعميم رقم {circular.id}")

        self.assertIn("لم أجد سجلاً مطابقًا", result["reply"])
        self.assertNotIn(circular.title, result["reply"])
        self.assertNotIn("مدير التعميم", result["reply"])

    def test_workflow_audit_uses_the_workflow_visibility_scope(self):
        request = WorkflowRequest(
            title="طلب متابعة المشاريع",
            status="IN_PROGRESS",
            requester_id=self.employee.id,
            confidentiality="NORMAL",
        )
        db.session.add(request)
        db.session.flush()
        db.session.add(AuditLog(
            request_id=request.id,
            user_id=self.issuer.id,
            action="WORKFLOW_COMMENT",
            note="تمت المتابعة مع الجهة المختصة.",
            created_at=datetime(2026, 9, 2, 10, 15),
        ))
        db.session.commit()

        result = collect_knowledge(self.employee, f"السجل الزمني للمعاملة رقم {request.id}")

        self.assertIn("طلب متابعة المشاريع", result["reply"])
        self.assertIn("مدير التعميم", result["reply"])
        self.assertIn("تمت المتابعة مع الجهة المختصة", result["reply"])


if __name__ == "__main__":
    unittest.main()
