import copy
import unittest

from flask import Flask

from extensions import db
from models import (
    EmployeeDependent,
    EmployeeFile,
    EmployeeQualification,
    HRLookupItem,
    User,
)
from services.employee_data_import import (
    EmployeeDataImportError,
    apply_employee_import_payload,
    build_employee_import_plan,
    canonical_payload_hash,
)


def answer(value, occurrence=1):
    return [{"value": value, "entry_id": f"field#{occurrence}", "occurrence": occurrence}]


class EmployeeDataImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY="test-only",
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
        db.drop_all()
        db.create_all()
        self.employee = self._user("employee@example.test", "موظف قديم")
        self.reviewer = self._user("hr@example.test", "موظف الموارد البشرية")
        self._lookup("GENDER", "M", "ذكر")
        self._lookup("DEP_RELATION", "SON", "ابن")
        self._lookup("QUAL_DEGREE", "BACHELOR", "بكالوريوس")
        db.session.commit()

    @staticmethod
    def _user(email, name):
        row = User(email=email, name=name, password_hash="unused", role="EMPLOYEE")
        db.session.add(row)
        db.session.flush()
        return row

    @staticmethod
    def _lookup(category, code, label):
        row = HRLookupItem(category=category, code=code, name_ar=label, is_active=True)
        db.session.add(row)
        db.session.flush()
        return row

    def payload(self):
        return {
            "schema": "EMP-DATA-FORM/V1.1",
            "exported_at": "2026-08-18T10:00:00Z",
            "employee": {"full_name_quad": "أحمد محمد علي حسن"},
            "fields": {
                "full_name_quad": answer("أحمد محمد علي حسن"),
                "employee_no": answer("E-100"),
                "national_id": answer("900000001"),
                "birth_date": answer("1990-02-03"),
                "gender_lookup_id": answer("ذكر"),
            },
            "tables": {
                "التابعون": [{
                    "dependent.full_name": "سارة أحمد حسن",
                    "dependent.relation_lookup_id": "ابن",
                    "dependent.national_id": "900000002",
                    "dependent.gender_lookup_id": "ذكر",
                    "dependent.birth_date": "2018-01-02",
                }],
                "المؤهلات": [{
                    "qualification.degree_lookup_id": "بكالوريوس",
                    "qualification.qualification_date": "2012-06-30",
                    "qualification.notes": "نسخة مصدقة",
                }],
            },
            "selections": [],
            "snapshot": {"entries": {"irrelevant": "value"}, "checks": {}},
        }

    def test_payload_hash_ignores_export_time_and_browser_snapshot(self):
        first = self.payload()
        second = copy.deepcopy(first)
        second["exported_at"] = "2026-08-19T11:00:00Z"
        second["snapshot"] = {"entries": {"different": "draft"}, "checks": {}}
        self.assertEqual(canonical_payload_hash(first), canonical_payload_hash(second))

    def test_plan_resolves_reference_values_and_previews_changes(self):
        plan = build_employee_import_plan(self.payload(), self.employee)
        self.assertEqual(plan["unresolved"], [])
        self.assertTrue(any(op["field"] == "gender_lookup_id" for op in plan["operations"]))
        self.assertEqual(len(plan["dependents"]), 1)
        self.assertEqual(len(plan["qualifications"]), 1)
        self.assertEqual(plan["dependents"][0]["action"], "create")

    def test_apply_updates_employee_file_and_is_idempotent_for_repeated_rows(self):
        summary = apply_employee_import_payload(
            self.payload(),
            self.employee,
            self.reviewer.id,
        )
        db.session.commit()

        employee_file = EmployeeFile.query.filter_by(user_id=self.employee.id).one()
        self.assertEqual(employee_file.employee_no, "E-100")
        self.assertEqual(employee_file.full_name_quad, "أحمد محمد علي حسن")
        self.assertEqual(employee_file.updated_by_id, self.reviewer.id)
        self.assertEqual(EmployeeDependent.query.filter_by(user_id=self.employee.id).count(), 1)
        self.assertEqual(EmployeeQualification.query.filter_by(user_id=self.employee.id).count(), 1)
        self.assertEqual(summary["dependents"]["created"], 1)

        second_summary = apply_employee_import_payload(
            self.payload(),
            self.employee,
            self.reviewer.id,
        )
        db.session.commit()
        self.assertEqual(EmployeeDependent.query.filter_by(user_id=self.employee.id).count(), 1)
        self.assertEqual(EmployeeQualification.query.filter_by(user_id=self.employee.id).count(), 1)
        self.assertEqual(second_summary["dependents"]["updated"], 1)
        self.assertEqual(second_summary["qualifications"]["updated"], 1)

    def test_apply_rejects_unresolved_values_without_writing_employee_file(self):
        payload = self.payload()
        payload["fields"]["gender_lookup_id"] = answer("قيمة غير معروفة")
        with self.assertRaises(EmployeeDataImportError):
            apply_employee_import_payload(payload, self.employee, self.reviewer.id)
        db.session.rollback()
        self.assertIsNone(EmployeeFile.query.filter_by(user_id=self.employee.id).first())


if __name__ == "__main__":
    unittest.main()
