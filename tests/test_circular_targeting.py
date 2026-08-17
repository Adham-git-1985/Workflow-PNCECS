import unittest

from flask import Flask

from extensions import db
from models import Department, Directorate, Organization, PortalCircular, User
from services.circulars import (
    can_user_view_circular,
    circular_recipient_user_ids,
    visible_circulars_query,
)


class CircularTargetingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
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
        cls.context.pop()

    def setUp(self):
        db.session.query(PortalCircular).delete()
        db.session.query(User).delete()
        db.session.query(Department).delete()
        db.session.query(Directorate).delete()
        db.session.query(Organization).delete()
        db.session.commit()

        organization = Organization(name_ar="اللجنة الوطنية", code="PNCECS")
        db.session.add(organization)
        db.session.flush()
        self.directorate_a = Directorate(
            organization_id=organization.id,
            name_ar="الإدارة أ",
            code="DIR-A",
        )
        self.directorate_b = Directorate(
            organization_id=organization.id,
            name_ar="الإدارة ب",
            code="DIR-B",
        )
        db.session.add_all([self.directorate_a, self.directorate_b])
        db.session.flush()
        self.department_a1 = Department(
            directorate_id=self.directorate_a.id,
            name_ar="الدائرة أ1",
            code="DEP-A1",
        )
        self.department_a2 = Department(
            directorate_id=self.directorate_a.id,
            name_ar="الدائرة أ2",
            code="DEP-A2",
        )
        self.department_b1 = Department(
            directorate_id=self.directorate_b.id,
            name_ar="الدائرة ب1",
            code="DEP-B1",
        )
        db.session.add_all([self.department_a1, self.department_a2, self.department_b1])
        db.session.flush()

        self.user_a1 = self._user("a1@example.test", department_id=self.department_a1.id)
        self.user_a2 = self._user("a2@example.test", department_id=self.department_a2.id)
        self.user_b1 = self._user("b1@example.test", department_id=self.department_b1.id)
        self.director_a = self._user(
            "director@example.test",
            directorate_id=self.directorate_a.id,
        )
        self.admin = self._user("admin@example.test", role="ADMIN")

        self.all_circular = PortalCircular(
            title="عام",
            body="للجميع",
            target_scope="ALL",
        )
        self.directorate_a_circular = PortalCircular(
            title="الإدارة أ",
            body="للإدارة أ",
            target_scope="DIRECTORATE",
            target_directorate_id=self.directorate_a.id,
        )
        self.directorate_b_circular = PortalCircular(
            title="الإدارة ب",
            body="للإدارة ب",
            target_scope="DIRECTORATE",
            target_directorate_id=self.directorate_b.id,
        )
        self.department_a1_circular = PortalCircular(
            title="الدائرة أ1",
            body="للدائرة أ1",
            target_scope="DEPARTMENT",
            target_department_id=self.department_a1.id,
        )
        self.department_a2_circular = PortalCircular(
            title="الدائرة أ2",
            body="للدائرة أ2",
            target_scope="DEPARTMENT",
            target_department_id=self.department_a2.id,
        )
        db.session.add_all([
            self.all_circular,
            self.directorate_a_circular,
            self.directorate_b_circular,
            self.department_a1_circular,
            self.department_a2_circular,
        ])
        db.session.commit()

    @staticmethod
    def _user(email, *, department_id=None, directorate_id=None, role="EMPLOYEE"):
        user = User(
            email=email,
            password_hash="not-used-in-test",
            role=role,
            department_id=department_id,
            directorate_id=directorate_id,
        )
        db.session.add(user)
        db.session.flush()
        return user

    def test_department_member_sees_own_department_and_parent_directorate(self):
        rows = visible_circulars_query(PortalCircular.query, self.user_a1).all()
        visible_ids = {row.id for row in rows}

        self.assertEqual(visible_ids, {
            self.all_circular.id,
            self.directorate_a_circular.id,
            self.department_a1_circular.id,
        })
        self.assertFalse(can_user_view_circular(self.department_a2_circular, self.user_a1))

    def test_explicit_directorate_member_sees_directorate_circular(self):
        self.assertTrue(can_user_view_circular(self.directorate_a_circular, self.director_a))
        self.assertFalse(can_user_view_circular(self.directorate_b_circular, self.director_a))

    def test_directorate_recipients_include_all_child_departments(self):
        recipient_ids = set(circular_recipient_user_ids(self.directorate_a_circular))

        self.assertIn(self.user_a1.id, recipient_ids)
        self.assertIn(self.user_a2.id, recipient_ids)
        self.assertIn(self.director_a.id, recipient_ids)
        self.assertNotIn(self.user_b1.id, recipient_ids)
        self.assertNotIn(self.admin.id, recipient_ids)

    def test_department_recipients_are_limited_to_selected_department(self):
        recipient_ids = set(circular_recipient_user_ids(self.department_a1_circular))

        self.assertEqual(recipient_ids, {self.user_a1.id})

    def test_circular_manager_can_review_every_audience(self):
        rows = visible_circulars_query(PortalCircular.query, self.admin).all()

        self.assertEqual(len(rows), 5)
        self.assertTrue(can_user_view_circular(self.department_a2_circular, self.admin))

    def test_invalid_stored_audience_fails_closed(self):
        invalid = PortalCircular(
            title="غير صالح",
            body="يجب ألا يوزع",
            target_scope="UNKNOWN",
        )

        self.assertFalse(can_user_view_circular(invalid, self.user_a1))
        self.assertEqual(circular_recipient_user_ids(invalid), [])


if __name__ == "__main__":
    unittest.main()
