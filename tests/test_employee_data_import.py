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
    EmployeeSecondment,
    HRLookupItem,
    Organization,
    OrgNode,
    OrgNodeManager,
    OrgNodeType,
    Section,
    Unit,
    User,
)
from services.employee_data_import import (
    EmployeeDataImportError,
    apply_employee_payload_corrections,
    apply_employee_import_payload,
    build_employee_import_plan,
    canonical_payload_hash,
)
from portal.routes import (
    _build_dynamic_employee_row,
    _dynamic_manager_columns,
    _resolve_employee_placement_ids,
    _timeclock_build_code_to_user,
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

    def test_questionnaire_placement_maps_labels_to_real_levels_and_parent_path(self):
        organization = Organization(name_ar="المؤسسة", is_active=True)
        db.session.add(organization)
        db.session.flush()
        directorate = Directorate(
            organization_id=organization.id,
            name_ar="الإدارة العامة المتخصصة",
            is_active=True,
        )
        other_directorate = Directorate(
            organization_id=organization.id,
            name_ar="إدارة عامة أخرى",
            is_active=True,
        )
        db.session.add_all([directorate, other_directorate])
        db.session.flush()
        department = Department(
            directorate_id=directorate.id,
            name_ar="دائرة الثقافة",
            is_active=True,
        )
        other_department = Department(
            directorate_id=other_directorate.id,
            name_ar="دائرة أخرى",
            is_active=True,
        )
        db.session.add_all([department, other_department])
        db.session.flush()
        section = Section(
            department_id=department.id,
            name_ar="قسم حماية التراث",
            is_active=True,
        )
        duplicate_section = Section(
            department_id=other_department.id,
            name_ar="قسم حماية التراث",
            is_active=True,
        )
        db.session.add_all([section, duplicate_section])
        db.session.commit()

        payload = self.payload()
        payload["fields"].update({
            "organization_id": answer("الإدارة العامة المتخصصة"),
            "directorate_id": answer("دائرة الثقافة"),
            "department_id": answer("قسم حماية التراث"),
        })

        plan = build_employee_import_plan(payload, self.employee)

        self.assertEqual(plan["unresolved"], [])
        resolved = {operation["field"]: operation["resolved"] for operation in plan["operations"]}
        self.assertEqual(resolved["organization_id"], organization.id)
        self.assertEqual(resolved["directorate_id"], directorate.id)
        self.assertEqual(resolved["department_id"], department.id)
        self.assertEqual(resolved["section_id"], section.id)

    def test_questionnaire_placement_accepts_a_unit_and_its_departments(self):
        organization = Organization(name_ar="المؤسسة", is_active=True)
        db.session.add(organization)
        db.session.flush()
        old_directorate = Directorate(
            organization_id=organization.id,
            name_ar="إدارة سابقة",
            is_active=True,
        )
        selected_unit = Unit(
            organization_id=organization.id,
            name_ar="وحدة البرامج",
            is_active=True,
        )
        other_unit = Unit(
            organization_id=organization.id,
            name_ar="وحدة الخدمات",
            is_active=True,
        )
        db.session.add_all((old_directorate, selected_unit, other_unit))
        db.session.flush()
        selected_department = Department(
            unit_id=selected_unit.id,
            name_ar="دائرة الدعم",
            is_active=True,
        )
        other_department = Department(
            unit_id=other_unit.id,
            name_ar="دائرة الدعم",
            is_active=True,
        )
        db.session.add_all((selected_department, other_department))
        db.session.add(EmployeeFile(
            user_id=self.employee.id,
            directorate_id=old_directorate.id,
        ))
        db.session.commit()

        payload = self.payload()
        payload["fields"].update({
            "organization_id": answer("وحدة البرامج"),
            "directorate_id": answer("دائرة الدعم"),
        })

        plan = build_employee_import_plan(payload, self.employee)
        resolved = {operation["field"]: operation["resolved"] for operation in plan["operations"]}

        self.assertEqual(plan["unresolved"], [])
        self.assertEqual(resolved["unit_id"], selected_unit.id)
        self.assertEqual(resolved["organization_id"], organization.id)
        self.assertIsNone(resolved["directorate_id"])
        self.assertEqual(resolved["department_id"], selected_department.id)

        apply_employee_import_payload(payload, self.employee, self.reviewer.id)
        employee_file = EmployeeFile.query.filter_by(user_id=self.employee.id).one()
        self.assertEqual(employee_file.organization_id, organization.id)
        self.assertIsNone(employee_file.directorate_id)
        self.assertEqual(employee_file.department_id, selected_department.id)

    def test_secondment_placement_matches_general_administration_and_circle(self):
        organization = Organization(name_ar="المؤسسة", is_active=True)
        db.session.add(organization)
        db.session.flush()
        directorate = Directorate(
            organization_id=organization.id,
            name_ar="الإدارة العامة للشؤون الإدارية والمالية",
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
        db.session.commit()

        payload = self.payload()
        payload["fields"].update({
            "secondment.organization_id": answer("الإدارة العامة للشؤون الإدارية والمالية"),
            "secondment.directorate_id": answer("دائرة الموارد البشرية"),
        })

        plan = build_employee_import_plan(payload, self.employee)

        self.assertEqual(plan["unresolved"], [])
        secondment_plan = plan["secondments"][0]
        self.assertEqual(secondment_plan["organization_id"], organization.id)
        self.assertEqual(secondment_plan["directorate_id"], directorate.id)
        self.assertEqual(secondment_plan["department_id"], department.id)

        apply_employee_import_payload(payload, self.employee, self.reviewer.id)
        secondment = EmployeeSecondment.query.filter_by(user_id=self.employee.id).one()
        self.assertEqual(secondment.organization_id, organization.id)
        self.assertEqual(secondment.directorate_id, directorate.id)
        self.assertEqual(secondment.department_id, department.id)

    def test_correction_fields_include_repeated_records_and_secondments(self):
        payload = self.payload()
        payload["tables"]["التابعون"][0]["dependent.relation_lookup_id"] = "ابنة"
        qualification = payload["tables"]["المؤهلات"][0]
        qualification.update({
            "qualification.specialization_lookup_id": "خدمة اجتماعية",
            "qualification.university_lookup_id": "جامعة القدس المفتوحة",
            "qualification.country_lookup_id": "فلسطين",
            "qualification.qualification_date": "2010",
        })
        payload["fields"].update({
            "secondment.organization_id": answer("الموارد البشرية والمالية"),
            "secondment.directorate_id": answer("الموارد البشرية"),
            "secondment.work_governorate_lookup_id": answer("رام الله"),
            "secondment.work_location_lookup_id": answer("البيرة"),
        })

        plan = build_employee_import_plan(payload, self.employee)
        labels = {item["label"] for item in plan["correction_fields"]}
        expected_labels = {
            "التابع 1: صلة القرابة",
            "المؤهل 1: التخصص",
            "المؤهل 1: الجامعة",
            "المؤهل 1: الدولة",
            "المؤهل 1: تاريخ المؤهل",
            "التكليف 1: الإدارة العامة",
            "التكليف 1: الدائرة",
            "التكليف 1: محافظة العمل",
            "التكليف 1: موقع العمل",
        }
        self.assertTrue(expected_labels.issubset(labels))

        replacements = {
            "التابع 1: صلة القرابة": "ابن",
            "المؤهل 1: التخصص": "علم الاجتماع",
            "المؤهل 1: الجامعة": "جامعة النجاح",
            "المؤهل 1: الدولة": "دولة فلسطين",
            "المؤهل 1: تاريخ المؤهل": "2010-01-01",
            "التكليف 1: الإدارة العامة": "إدارة الموارد البشرية",
            "التكليف 1: الدائرة": "دائرة شؤون الموظفين",
            "التكليف 1: محافظة العمل": "رام الله والبيرة",
            "التكليف 1: موقع العمل": "المقر الرئيسي",
        }
        form_values = {
            f"correction_{index}": replacements.get(correction["label"], correction["value"])
            for index, correction in enumerate(plan["correction_fields"])
        }

        changed = apply_employee_payload_corrections(payload, plan["correction_fields"], form_values)

        self.assertEqual(changed, len(replacements))
        self.assertEqual(payload["tables"]["التابعون"][0]["dependent.relation_lookup_id"], "ابن")
        self.assertEqual(qualification["qualification.specialization_lookup_id"], "علم الاجتماع")
        self.assertEqual(qualification["qualification.qualification_date"], "2010-01-01")
        self.assertEqual(payload["fields"]["secondment.organization_id"][0]["value"], "إدارة الموارد البشرية")
        self.assertEqual(payload["fields"]["secondment.work_location_lookup_id"][0]["value"], "المقر الرئيسي")

    def test_manager_resolution_accepts_a_unique_abbreviated_arabic_name(self):
        manager = self._user("manager@example.test", "خلود احمد يوسف حنتش")
        db.session.commit()
        payload = self.payload()
        payload["fields"]["direct_manager_user_id"] = answer("خلود حنتش")

        plan = build_employee_import_plan(payload, self.employee)

        self.assertEqual(plan["unresolved"], [])
        operation = next(
            item for item in plan["operations"]
            if item["field"] == "direct_manager_user_id"
        )
        self.assertEqual(operation["resolved"], manager.id)

    def test_missing_qualification_lookups_can_be_created_during_apply(self):
        payload = self.payload()
        payload["tables"]["المؤهلات"][0].update({
            "qualification.specialization_lookup_id": "علم الآثار",
            "qualification.grade_lookup_id": "جيد جدا",
            "qualification.university_lookup_id": "جامعة القدس",
        })

        preview = build_employee_import_plan(payload, self.employee)
        self.assertEqual(len(preview["unresolved"]), 3)

        apply_plan = build_employee_import_plan(
            payload,
            self.employee,
            create_missing_lookups=True,
        )
        self.assertEqual(apply_plan["unresolved"], [])
        self.assertEqual(
            {item["category"] for item in apply_plan["created_lookups"]},
            {"QUAL_SPECIALIZATION", "QUAL_GRADE", "UNIVERSITY"},
        )

    def test_missing_lookup_values_can_be_created_before_other_errors_are_fixed(self):
        payload = self.payload()
        payload["fields"].update({
            "secondment.date_from": answer("2010"),
            "secondment.work_governorate_lookup_id": answer("رام الله"),
            "secondment.work_location_lookup_id": answer("البيرة"),
        })

        plan = build_employee_import_plan(
            payload,
            self.employee,
            create_missing_lookups=True,
        )

        self.assertTrue(any(issue["field"] == "التكليف 1: من تاريخ" for issue in plan["unresolved"]))
        self.assertEqual(
            {item["category"] for item in plan["created_lookups"]},
            {"WORK_GOV", "WORK_LOCATION"},
        )

        db.session.commit()
        self.assertIsNotNone(HRLookupItem.query.filter_by(category="WORK_GOV", name_ar="رام الله").first())
        self.assertIsNotNone(HRLookupItem.query.filter_by(category="WORK_LOCATION", name_ar="البيرة").first())

    def test_manager_chain_uses_dynamic_nodes_and_custom_levels(self):
        directorate_type = OrgNodeType(
            code="DIRECTORATE",
            name_ar="إدارة عامة",
            sort_order=20,
            is_active=True,
        )
        section_type = OrgNodeType(
            code="SECTION",
            name_ar="قسم",
            sort_order=50,
            is_active=True,
        )
        db.session.add_all([directorate_type, section_type])
        db.session.flush()
        directorate = OrgNode(
            type_id=directorate_type.id,
            name_ar="الإدارة العامة للبرامج",
            is_active=True,
        )
        db.session.add(directorate)
        db.session.flush()
        section = OrgNode(
            type_id=section_type.id,
            parent_id=directorate.id,
            name_ar="قسم البرامج",
            is_active=True,
        )
        section_manager = self._user("section-manager@example.test", "مسؤول القسم")
        directorate_manager = self._user("directorate-manager@example.test", "مسؤول الإدارة")
        db.session.add(section)
        db.session.flush()
        db.session.add_all([
            OrgNodeManager(node_id=section.id, manager_user_id=section_manager.id),
            OrgNodeManager(node_id=directorate.id, manager_user_id=directorate_manager.id),
        ])
        db.session.commit()

        nodes = {node.id: node for node in OrgNode.query.all()}
        managers = {row.node_id: row for row in OrgNodeManager.query.all()}
        row = _build_dynamic_employee_row(self.employee, section, nodes, managers)
        columns = _dynamic_manager_columns(nodes, managers)

        self.assertEqual(row["assigned_unit"], "قسم: قسم البرامج")
        self.assertEqual(row["manager_by_type"]["SECTION"], "مسؤول القسم")
        self.assertEqual(row["manager_by_type"]["DIRECTORATE"], "مسؤول الإدارة")
        self.assertIn("قسم: مسؤول القسم", row["chain_text"])
        self.assertEqual([column["code"] for column in columns], ["SECTION", "DIRECTORATE"])

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
