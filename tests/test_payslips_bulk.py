import tempfile
import unittest
from pathlib import Path

import fitz
from flask import Flask
from flask_login import LoginManager, login_user

from extensions import db
from models import EmployeeAttachment, EmployeeFile, User
from portal.payslips_bulk import _split_and_register_payslip_pdf


class PayslipsBulkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.app = Flask(__name__, instance_path=cls.temp_dir.name)
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="payslips-bulk-test",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(cls.app)
        login_manager = LoginManager()
        login_manager.init_app(cls.app)

        @login_manager.user_loader
        def load_user(user_id):
            return db.session.get(User, int(user_id))

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

        self.uploader = User(
            email="hr@example.test",
            name="HR",
            password_hash="x",
            role="HR",
        )
        self.employee = User(
            email="employee@example.test",
            name="Employee",
            password_hash="x",
            role="employee",
        )
        db.session.add_all((self.uploader, self.employee))
        db.session.flush()
        db.session.add(EmployeeFile(
            user_id=self.employee.id,
            employee_no="187464",
            full_name_quad="موظف الاختبار",
            national_id="412578742",
        ))
        db.session.commit()

    def test_groups_matching_identity_pages_into_one_payslip(self):
        source_pdf = Path(self.temp_dir.name) / "source.pdf"
        output_dir = Path(self.temp_dir.name) / "output"
        source = fitz.open()
        try:
            for text in (
                "Identity 412578742\nFirst payslip page",
                "Identity 412578742\nSecond payslip page",
                "Identity 987654321\nUnmatched payslip page",
            ):
                page = source.new_page()
                page.insert_text((72, 72), text)
            source.save(str(source_pdf))
        finally:
            source.close()

        with self.app.test_request_context("/"):
            login_user(self.uploader)
            rows, warnings, counters = _split_and_register_payslip_pdf(
                source_pdf,
                output_dir,
                year=2026,
                month=5,
            )
            db.session.commit()

        saved_row = next(row for row in rows if row["status"] == "saved")
        attachment = EmployeeAttachment.query.filter_by(
            user_id=self.employee.id,
            attachment_type="PAYSLIP",
            payslip_year=2026,
            payslip_month=5,
        ).one()
        merged_pdf = output_dir / saved_row["filename"]

        self.assertEqual(counters, {"saved": 1, "skipped": 1, "pages": 3})
        self.assertEqual(len(rows), 2)
        self.assertEqual(saved_row["page_numbers"], [1, 2])
        self.assertEqual(saved_row["identity_number"], "412578742")
        self.assertIn("صفحة", saved_row["message"])
        self.assertIn("1", attachment.note)
        self.assertIn("2", attachment.note)

        merged_document = fitz.open(str(merged_pdf))
        try:
            self.assertEqual(merged_document.page_count, 2)
            self.assertIn("First payslip page", merged_document[0].get_text())
            self.assertIn("Second payslip page", merged_document[1].get_text())
        finally:
            merged_document.close()


if __name__ == "__main__":
    unittest.main()
