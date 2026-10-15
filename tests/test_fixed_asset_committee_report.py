import io
import unittest

from flask import g

from extensions import db
from models import (
    InvAssetDocument,
    InvFixedAssetCycle,
    InvFixedAssetCycleMember,
    InvFixedAssetEntry,
    InvFixedAssetPreviousLine,
    User,
)
from services.asset_custody import review_inventory_document, send_cycle_reports
from services.fixed_asset_inventory import close_inventory_cycle, populate_cycle_entries, record_asset_scan
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
        close_inventory_cycle(cycle, closed_by_id=self.admin.id)
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
        preview = self.client.get(f"{report_url}/print")
        self.assertEqual(preview.status_code, 200)
        self.assertIn("تقرير الفروقات", preview.get_data(as_text=True))
        response = self.client.post(report_url, data={"action": "submit", "summary": "ملخص اللجنة"})
        self.assertEqual(response.status_code, 302)

        self.login(self.secretary)
        page = self.client.get(report_url)
        self.assertEqual(page.status_code, 200)
        response = self.client.post(report_url, data={"action": "approve", "secretary_note": "معتمد"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(doc.status, "APPROVED")
        self.assertEqual(doc.employee_response, "ACCEPT")
        self.assertIsNone(asset.custodian_user_id)
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


if __name__ == "__main__":
    unittest.main()
