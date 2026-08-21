import copy
import unittest

from flask import Flask

from extensions import db
from models import (
    Department,
    Directorate,
    Division,
    EmployeeDependent,
    EmployeeFile,
    EmployeeQualification,
    HRLookupItem,
    Organization,
    Section,
    User,
)
from services.employee_data_import import (
    EmployeeDataImportError,
    apply_employee_import_payload,
    build_employee_import_plan,
    canonical_payload_hash,
)
from portal.routes import _resolve_employee_placement_ids, _timeclock_build_code_to_user


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

    def test_plan_normalizes_common_date_formats(self):
        payload = self.payload()
        payload["fields"]["birth_date"] = answer("11-08-1985")
        next(iter(payload["tables"].values()))[0]["dependent.birth_date"] = "٠٢/٠١/٢٠١٨"

        plan = build_employee_import_plan(payload, self.employee)

        self.assertEqual(plan["unresolved"], [])
        birth_operation = next(item for item in plan["operations"] if item["field"] == "birth_date")
        self.assertEqual(birth_operation["resolved"], "1985-08-11")
        self.assertEqual(plan["dependents"][0]["birth_date"], "2018-01-02")

    def test_plan_accepts_short_numeric_timeclock_code(self):
        payload = self.payload()
        payload["fields"]["timeclock_code"] = answer("67300")

        plan = build_employee_import_plan(payload, self.employee)

        self.assertEqual(plan["unresolved"], [])
        timeclock_operation = next(item for item in plan["operations"] if item["field"] == "timeclock_code")
        self.assertEqual(timeclock_operation["resolved"], "67300")

    def test_auto_timeclock_matching_uses_employee_and_national_identifiers(self):
        db.session.add(EmployeeFile(
            user_id=self.employee.id,
            employee_no="67300",
            national_id="99887766",
            timeclock_code="67300",
        ))
        db.session.commit()

        matches = _timeclock_build_code_to_user("AUTO")

        self.assertEqual(matches["67300"], self.employee.id)
        self.assertEqual(matches["99887766"], self.employee.id)

    def test_employee_placement_keeps_each_hierarchy_level_in_its_correct_field(self):
        organization = Organization(name_ar="المؤسسة", is_active=True)
        db.session.add(organization)
        db.session.flush()
        directorate = Directorate(
            organization_id=organization.id,
            name_ar="الإدارة العامة للموارد",
            is_active=True,
        )
        db.session.add(directorate)
        db.session.flush()
        department = Department(
            directorate_id=directorate.id,
            name_ar="دائرة الموارد البشرية",
            is_active=True,
        )
        db.session.add(department)
        db.session.flush()
        section = Section(
            department_id=department.id,
            name_ar="قسم شؤون الموظفين",
            is_active=True,
        )
        db.session.add(section)
        db.session.flush()
        division = Division(
            section_id=section.id,
            name_ar="شعبة الملفات",
            is_active=True,
        )
        db.session.add(division)
        db.session.flush()

        placement = _resolve_employee_placement_ids(division_id=division.id)

        self.assertEqual(placement["organization_id"], organization.id)
        self.assertEqual(placement["directorate_id"], directorate.id)
        self.assertEqual(placement["department_id"], department.id)
        self.assertEqual(placement["section_id"], section.id)
        self.assertEqual(placement["division_id"], division.id)

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
