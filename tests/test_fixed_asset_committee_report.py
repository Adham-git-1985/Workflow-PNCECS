import io
import unittest
from pathlib import Path

from flask import g
from openpyxl import load_workbook
from PIL import Image

from extensions import db
from models import (
    InvAssetDocument,
    InvFixedAssetCycle,
    InvFixedAssetCycleMember,
    InvFixedAssetEntry,
    InvFixedAssetPreviousLine,
    InvItem,
    InvItemCategory,
    InvRoom,
    User,
)
from services.asset_custody import (
    get_or_create_committee_report,
    review_inventory_document,
    revise_inventory_document,
    send_cycle_reports,
    submit_committee_report,
)
from services.fixed_asset_inventory import populate_cycle_entries, record_asset_scan
import tests.test_fixed_asset_routes as fixtures


class FixedAssetCommitteeReportTests(unittest.TestCase):
    setUpClass = fixtures.FixedAssetRouteTests.__dict__["setUpClass"]
    tearDownClass = fixtures.FixedAssetRouteTests.__dict__["tearDownClass"]
    _create_asset = fixtures.FixedAssetRouteTests._create_asset

    def setUp(self):
        fixtures.FixedAssetRouteTests.setUp(self)
        self.employee = User(
            email="committee-holder@example.test",
            name="موظف اللجنة",
            role="EMPLOYEE",
            password_hash="unused",
        )
        self.secretary = User(
            email="secretary-general@example.test",
            name="الأمين العام",
            role="SECRETARY_GENERAL",
            password_hash="unused",
        )
        db.session.add_all([self.employee, self.secretary])
        db.session.commit()
        self.login(self.admin)

    def login(self, user):
        for key in list(g):
            g.pop(key, None)
        with self.client.session_transaction() as session:
            session["_user_id"] = str(user.id)
            session["_fresh"] = True

    def make_cycle(self):
        asset = self._create_asset()
        cycle = InvFixedAssetCycle(
            code="COMMITTEE-2026",
            name="جرد لجنة 2026",
            inventory_date="2026-09-11",
            committee_name="لجنة الجرد الثابت 2026",
            status="ACTIVE",
        )
        db.session.add(cycle)
        db.session.flush()
        db.session.add(InvFixedAssetCycleMember(cycle_id=cycle.id, user_id=self.admin.id, member_role="CHAIR"))
        populate_cycle_entries(cycle)
        record_asset_scan(
            cycle,
            asset,
            scanned_by_id=self.admin.id,
            observed_warehouse_id=self.warehouse.id,
            observed_room_id=None,
            observed_custodian_user_id=self.employee.id,
            observed_condition="GOOD",
            observed_lifecycle_status="ACTIVE",
        )
        db.session.commit()
        return cycle, asset

    def send_one(self):
        cycle, asset = self.make_cycle()
        doc = send_cycle_reports(cycle, self.admin.id)[0]
        db.session.commit()
        return cycle, asset, doc

    def test_committee_member_can_send_employee_custodies(self):
        cycle, asset = self.make_cycle()
        db.session.add(InvFixedAssetCycleMember(
            cycle_id=cycle.id,
            user_id=self.employee.id,
            member_role="MEMBER",
        ))
        db.session.commit()
        self.login(self.employee)
        response = self.client.post(
            f"/portal/inventory/fixed-assets/cycles/{cycle.id}/send-reports"
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(InvAssetDocument.query.count(), 1)

    def test_manual_custody_assignment_counts_as_inventory_without_qr(self):
        asset = self._create_asset("FA-MANUAL-001")
        asset.custodian_user_id = self.employee.id
        cycle = InvFixedAssetCycle(
            code="COMMITTEE-MANUAL-2026",
            name="جرد يدوي 2026",
            inventory_date="2026-09-13",
            committee_name="لجنة الجرد اليدوي",
            status="ACTIVE",
        )
        db.session.add(cycle)
        db.session.flush()
        populate_cycle_entries(cycle)
        db.session.commit()

        doc = send_cycle_reports(cycle, self.admin.id)[0]
        db.session.commit()
        entry = InvFixedAssetEntry.query.filter_by(cycle_id=cycle.id, asset_id=asset.id).one()
        self.assertIsNotNone(entry.scanned_at)
        self.assertEqual(entry.result_status, "MATCHED")
        self.assertEqual(doc.lines[0].snapshot["result"], "MATCHED")

    def test_missing_previous_year_does_not_create_false_variances(self):
        cycle, asset, doc = self.send_one()
        self.login(self.admin)
        report_url = f"/portal/inventory/fixed-assets/cycles/{cycle.id}/committee-report"
        self.assertEqual(self.client.get(report_url).status_code, 200)
        preview = self.client.get(
            f"/portal/inventory/fixed-assets/cycles/{cycle.id}/committee-report/print"
        )
        self.assertEqual(preview.status_code, 200)
        preview_text = preview.get_data(as_text=True)
        self.assertIn("لا توجد بيانات سابقة للمقارنة", preview_text)
        self.assertNotIn('data-has-difference="1"', preview_text)

    def test_committee_report_print_loads_only_selected_employee(self):
        cycle, asset, doc = self.send_one()
        other = User(
            email="committee-holder-two@example.test",
            name="موظف آخر في التقرير",
            role="EMPLOYEE",
            password_hash="unused",
        )
        db.session.add(other)
        db.session.flush()
        other_asset = self._create_asset("FA-REPORT-OTHER-001")
        other_asset.custodian_user_id = other.id
        db.session.commit()
        populate_cycle_entries(cycle)
        db.session.commit()
        send_cycle_reports(cycle, self.admin.id)
        db.session.commit()

        self.login(self.admin)
        report_url = f"/portal/inventory/fixed-assets/cycles/{cycle.id}/committee-report"
        self.assertEqual(self.client.get(report_url).status_code, 200)
        full_text = self.client.get(
            f"{report_url}/print"
        ).get_data(as_text=True)
        selected_text = self.client.get(
            f"{report_url}/print?employee_id={self.employee.id}#employee-{self.employee.id}"
        ).get_data(as_text=True)
        self.assertIn(other.full_name, full_text)
        self.assertNotIn(other.full_name, selected_text)
        self.assertIn(self.employee.full_name, selected_text)

    def test_employee_change_then_committee_revise_and_reject_escalate(self):
        cycle, asset, doc = self.send_one()
        review_inventory_document(doc, self.employee.id, "CHANGE", "الموقع يحتاج تصحيحاً")
        db.session.commit()
        self.assertEqual(doc.status, "CHANGE_REQUESTED")

        self.login(self.admin)
        line = doc.lines[0]
        response = self.client.post(
            f"/portal/inventory/fixed-assets/documents/{doc.id}",
            data={
                "action": "revise",
                f"include_{line.id}": "1",
                f"tag_{line.id}": line.snapshot["tag"],
                f"name_{line.id}": line.snapshot["name"],
                f"serial_{line.id}": "SN-UPDATED",
                f"location_{line.id}": "موقع مصحح",
                f"condition_{line.id}": "GOOD",
                f"lifecycle_{line.id}": "ACTIVE",
                "committee_revision_note": "تم التصحيح من اللجنة",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(doc.status, "PENDING")
        self.assertEqual(doc.revision_number, 1)

        self.login(self.employee)
        response = self.client.post(
            f"/portal/inventory/fixed-assets/documents/{doc.id}",
            data={
                "action": "review",
                "inventory_action": "REJECT",
                "employee_note": "الأصل ليس بعهدتي",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(doc.status, "REJECTED")

        self.login(self.admin)
        response = self.client.post(
            f"/portal/inventory/fixed-assets/documents/{doc.id}",
            data={"action": "escalate", "escalation_note": "يرجى توجيه الأمين العام"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(doc.status, "REJECTED_ESCALATED")
        self.assertTrue(any(event.action == "REJECTION_ESCALATED" for event in doc.events))

    def test_committee_report_submission_and_secretary_approval(self):
        cycle, asset, doc = self.send_one()
        review_inventory_document(doc, self.employee.id, "ACCEPT", "تمت المطابقة")
        db.session.commit()

        self.login(self.admin)
        report_url = f"/portal/inventory/fixed-assets/cycles/{cycle.id}/committee-report"
        page = self.client.get(report_url)
        self.assertEqual(page.status_code, 200)
        self.assertIn("تقرير لجنة الجرد الثابت", page.get_data(as_text=True))
        self.assertIn("وافق الموظف", page.get_data(as_text=True))
        self.assertIn("الأمين العام", page.get_data(as_text=True))
        self.assertIn("لم يتم إرساله للأمين العام بعد", page.get_data(as_text=True))
        preview = self.client.get(f"{report_url}/print")
        self.assertEqual(preview.status_code, 200)
        self.assertIn("تقرير الفروقات", preview.get_data(as_text=True))
        self.assertIn("بحث متقدم داخل التقرير", preview.get_data(as_text=True))
        self.assertIn("فلتر الفروقات", preview.get_data(as_text=True))
        response = self.client.post(report_url, data={"action": "submit", "summary": "ملخص اللجنة"})
        self.assertEqual(response.status_code, 302)
        documents_page = self.client.get(f"/portal/inventory/fixed-assets/documents?cycle_id={cycle.id}")
        self.assertEqual(documents_page.status_code, 200)
        self.assertIn("مرفوع للأمين العام", documents_page.get_data(as_text=True))
        self.assertIn("قراءة التقرير", documents_page.get_data(as_text=True))
        self.login(self.employee)
        custody_page = self.client.get(f"/portal/inventory/fixed-assets/documents/{doc.id}")
        self.assertEqual(custody_page.status_code, 200)
        self.assertIn("تم إرساله للأمين العام", custody_page.get_data(as_text=True))

        self.login(self.secretary)
        page = self.client.get(report_url)
        self.assertEqual(page.status_code, 200)
        response = self.client.post(report_url, data={"action": "approve", "secretary_note": "معتمد"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(doc.status, "APPROVED")
        self.assertEqual(doc.employee_response, "ACCEPT")
        self.assertIsNone(asset.custodian_user_id)
        self.assertEqual(cycle.committee_report.status, "READY_TO_CLOSE")
        self.assertEqual(cycle.status, "ACTIVE")
        self.assertIsNone(cycle.finalized_at)

        self.login(self.admin)
        close_response = self.client.post(
            f"/portal/inventory/fixed-assets/cycles/{cycle.id}/close",
        )
        self.assertEqual(close_response.status_code, 302)
        self.assertEqual(cycle.committee_report.status, "CLOSED")
        self.assertEqual(cycle.status, "CLOSED")
        self.assertIsNotNone(cycle.finalized_at)
        next_cycle = cycle.successor_cycle
        self.assertIsNotNone(next_cycle)
        self.assertEqual(next_cycle.previous_cycle_id, cycle.id)
        self.assertEqual(next_cycle.status, "ACTIVE")
        self.assertEqual([entry.asset_id for entry in next_cycle.entries], [asset.id])

        self.login(self.admin)
        response = self.client.post(
            f"/portal/inventory/fixed-assets/documents/{doc.id}",
            data={"action": "issue"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(doc.status, "ISSUED")
        self.assertEqual(asset.custodian_user_id, self.employee.id)

        pdf = self.client.get(f"/portal/inventory/fixed-assets/cycles/{cycle.id}/committee-report.pdf")
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.mimetype, "application/pdf")
        self.assertTrue(pdf.data.startswith(b"%PDF"))

    def test_manual_previous_lines_are_saved_for_variance_report(self):
        cycle, asset, doc = self.send_one()
        self.login(self.admin)
        report_url = f"/portal/inventory/fixed-assets/cycles/{cycle.id}/committee-report"
        self.assertEqual(self.client.get(report_url).status_code, 200)
        response = self.client.post(
            f"{report_url}/previous/{self.employee.id}",
            data={"previous_data": "FA-OLD | أصل سابق | OLD-SN | مكتب قديم | FAIR | يحتاج متابعة"},
        )
        self.assertEqual(response.status_code, 302)
        previous = InvFixedAssetPreviousLine.query.one()
        self.assertEqual(previous.employee_id, self.employee.id)
        self.assertEqual(previous.snapshot["location"], "مكتب قديم")
        preview = self.client.get(f"{report_url}/print")
        self.assertEqual(preview.status_code, 200)
        self.assertIn("تفاصيل الفروقات", preview.get_data(as_text=True))
        self.assertIn("الرقم التسلسلي", preview.get_data(as_text=True))

    def test_document_and_committee_report_show_complete_asset_details(self):
        cycle, asset, doc = self.send_one()
        category = InvItemCategory(name="أجهزة مكتبية")
        item = InvItem(name="حاسوب محمول", code="IT-2026", category=category)
        room = InvRoom(name="غرفة الاجتماعات", code="R-12")
        asset.serial_number = "SN-REPORT-2026"
        asset.description = "جهاز محمول مع حقيبة وملحقات"
        asset.category = category
        asset.item = item
        asset.room = room
        asset.image_filename = "committee-report-asset.png"
        db.session.add_all([category, item, room])
        db.session.commit()
        image_path = Path(self.app.instance_path) / "uploads" / "fixed_assets" / asset.image_filename
        image_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (6, 6), (32, 112, 160)).save(image_path, format="PNG")

        self.login(self.employee)
        document_page = self.client.get(f"/portal/inventory/fixed-assets/documents/{doc.id}")
        document_text = document_page.get_data(as_text=True)
        self.assertEqual(document_page.status_code, 200)
        self.assertIn("SN-REPORT-2026", document_text)
        self.assertIn("صنف المستودع", document_text)
        self.assertIn("حاسوب محمول", document_text)
        self.assertIn("غرفة الاجتماعات", document_text)
        self.assertIn("جهاز محمول مع حقيبة وملحقات", document_text)
        documents_page = self.client.get("/portal/inventory/fixed-assets/documents")
        documents_text = documents_page.get_data(as_text=True)
        self.assertEqual(documents_page.status_code, 200)
        self.assertIn("فتح العهدة والتفاصيل", documents_text)
        self.assertIn("1 أصل", documents_text)
        self.assertNotIn("SN-REPORT-2026", documents_text)
        self.assertEqual(
            self.client.get(f"/portal/inventory/fixed-assets/assets/{asset.id}/image").status_code,
            200,
        )

        self.login(self.admin)
        report_url = f"/portal/inventory/fixed-assets/cycles/{cycle.id}/committee-report"
        self.assertEqual(self.client.get(report_url).status_code, 200)
        preview = self.client.get(f"/portal/inventory/fixed-assets/cycles/{cycle.id}/committee-report/print")
        preview_text = preview.get_data(as_text=True)
        self.assertEqual(preview.status_code, 200)
        for value in ("SN-REPORT-2026", "حاسوب محمول", "أجهزة مكتبية", "غرفة الاجتماعات", "جهاز محمول مع حقيبة وملحقات"):
            self.assertIn(value, preview_text)
        self.login(self.secretary)
        self.assertEqual(
            self.client.get(f"/portal/inventory/fixed-assets/assets/{asset.id}/image").status_code,
            200,
        )
        self.assertEqual(
            self.client.get(f"/portal/inventory/fixed-assets/cycles/{cycle.id}/committee-report/print").status_code,
            200,
        )
        workbook = load_workbook(
            io.BytesIO(self.client.get(f"/portal/inventory/fixed-assets/cycles/{cycle.id}/committee-report.xlsx").data),
            read_only=True,
        )
        custody_sheet = workbook["العهد والأصول"]
        header = next(custody_sheet.iter_rows(values_only=True))
        self.assertIn("صنف المستودع", header)
        self.assertIn("الوصف", header)
        rows = list(custody_sheet.iter_rows(values_only=True))
        self.assertIn("SN-REPORT-2026", [row[9] for row in rows[1:]])
        pdf = self.client.get(f"/portal/inventory/fixed-assets/cycles/{cycle.id}/committee-report.pdf")
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.data.startswith(b"%PDF"))

    def test_open_cycle_can_send_custody_and_report_to_secretary(self):
        asset = self._create_asset("FA-OPEN-001")
        cycle = InvFixedAssetCycle(
            code="COMMITTEE-OPEN-2026",
            name="جرد مفتوح 2026",
            inventory_date="2026-09-12",
            committee_name="لجنة الجرد الثابت 2026",
            status="ACTIVE",
        )
        db.session.add(cycle)
        db.session.flush()
        db.session.add(InvFixedAssetCycleMember(cycle_id=cycle.id, user_id=self.admin.id, member_role="CHAIR"))
        populate_cycle_entries(cycle)
        record_asset_scan(
            cycle,
            asset,
            scanned_by_id=self.admin.id,
            observed_warehouse_id=self.warehouse.id,
            observed_room_id=None,
            observed_custodian_user_id=self.employee.id,
            observed_condition="GOOD",
            observed_lifecycle_status="ACTIVE",
        )
        db.session.commit()

        doc = send_cycle_reports(cycle, self.admin.id)[0]
        db.session.commit()
        self.assertEqual(cycle.status, "ACTIVE")
        review_inventory_document(doc, self.employee.id, "ACCEPT", "تمت المطابقة")
        report = get_or_create_committee_report(cycle, self.admin.id)
        submit_committee_report(report, self.admin.id)
        db.session.commit()
        self.assertEqual(report.status, "SUBMITTED")

        export = self.client.get(f"/portal/inventory/fixed-assets/cycles/{cycle.id}/committee-report.xlsx")
        self.assertEqual(export.status_code, 200)
        self.assertIn("spreadsheetml", export.mimetype)
        workbook = load_workbook(io.BytesIO(export.data), read_only=True)
        self.assertEqual(
            set(workbook.sheetnames),
            {"ملخص التقرير", "العهد والأصول", "الفروقات", "السجل"},
        )

        self.login(self.secretary)
        response = self.client.post(
            f"/portal/inventory/fixed-assets/cycles/{cycle.id}/committee-report",
            data={"action": "approve", "secretary_note": "معتمد"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(report.status, "READY_TO_CLOSE")
        self.assertEqual(cycle.status, "ACTIVE")
        self.assertIsNone(cycle.finalized_at)
        self.login(self.admin)
        close_response = self.client.post(
            f"/portal/inventory/fixed-assets/cycles/{cycle.id}/close",
        )
        self.assertEqual(close_response.status_code, 302)
        self.assertEqual(cycle.status, "CLOSED")
        self.assertIsNotNone(cycle.finalized_at)

    def test_committee_can_remove_and_add_asset_before_final_approval(self):
        first = self._create_asset("FA-EDIT-001")
        second = self._create_asset("FA-EDIT-002")
        cycle = InvFixedAssetCycle(
            code="COMMITTEE-EDIT-2026",
            name="جرد تعديل 2026",
            inventory_date="2026-09-13",
            committee_name="لجنة الجرد الثابت 2026",
            status="ACTIVE",
        )
        db.session.add(cycle)
        db.session.flush()
        db.session.add(InvFixedAssetCycleMember(cycle_id=cycle.id, user_id=self.admin.id, member_role="CHAIR"))
        populate_cycle_entries(cycle)
        record_asset_scan(
            cycle,
            first,
            scanned_by_id=self.admin.id,
            observed_warehouse_id=self.warehouse.id,
            observed_room_id=None,
            observed_custodian_user_id=self.employee.id,
            observed_condition="GOOD",
            observed_lifecycle_status="ACTIVE",
        )
        db.session.commit()
        doc = send_cycle_reports(cycle, self.admin.id)[0]
        db.session.commit()
        review_inventory_document(doc, self.employee.id, "ACCEPT", "تمت المطابقة")
        db.session.commit()

        report = get_or_create_committee_report(cycle, self.admin.id)
        submit_committee_report(report, self.admin.id)
        db.session.commit()

        record_asset_scan(
            cycle,
            second,
            scanned_by_id=self.admin.id,
            observed_warehouse_id=self.warehouse.id,
            observed_room_id=None,
            observed_custodian_user_id=self.employee.id,
            observed_condition="GOOD",
            observed_lifecycle_status="ACTIVE",
        )
        db.session.commit()

        line = next(line for line in doc.lines if line.asset_id == first.id)
        revise_inventory_document(
            doc,
            self.admin.id,
            {
                line.id: {"included": False},
                "__new_asset_ids": [str(second.id)],
            },
            "تعديل اللجنة حسب المراجعة",
        )
        db.session.commit()
        self.assertEqual(doc.status, "PENDING")
        self.assertIsNone(doc.employee_response)
        self.assertFalse(next(line for line in doc.lines if line.asset_id == first.id).snapshot["included"])
        self.assertTrue(any(line.asset_id == second.id for line in doc.lines))
        self.assertTrue(any(event.action == "COMMITTEE_REVISED" for event in doc.events))

        review_inventory_document(doc, self.employee.id, "ACCEPT", "تم اعتماد التعديل")
        db.session.commit()
        self.login(self.secretary)
        response = self.client.post(
            f"/portal/inventory/fixed-assets/cycles/{cycle.id}/committee-report",
            data={"action": "approve", "secretary_note": "معتمد بعد التعديل"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(report.status, "READY_TO_CLOSE")
        self.assertEqual(cycle.status, "ACTIVE")
        self.login(self.admin)
        close_response = self.client.post(
            f"/portal/inventory/fixed-assets/cycles/{cycle.id}/close",
        )
        self.assertEqual(close_response.status_code, 302)
        self.assertEqual(report.status, "CLOSED")
        self.assertEqual(cycle.status, "CLOSED")


if __name__ == "__main__":
    unittest.main()
