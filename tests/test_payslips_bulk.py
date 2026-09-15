import tempfile
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import fitz
from flask import Flask
from flask_login import LoginManager, login_user
from jinja2 import ChoiceLoader, DictLoader
from werkzeug.datastructures import FileStorage, MultiDict

from extensions import db
from models import EmployeeAttachment, EmployeeFile, User
from portal import portal_bp
from portal.payslips_bulk import (
    _combine_payslip_pdf_files,
    _extract_identity_number,
    _find_employee_by_identity,
    _payslip_page_text,
    _split_and_register_payslip_pdf,
    _uploaded_payslip_files,
)


class PayslipsBulkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        project_root = Path(__file__).resolve().parents[1]
        cls.app = Flask(
            __name__,
            instance_path=cls.temp_dir.name,
            template_folder=str(project_root / "templates"),
        )
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="payslips-bulk-test",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            ARCHIVE_STORAGE_DIR=str(Path(cls.temp_dir.name) / "archive"),
        )
        db.init_app(cls.app)
        login_manager = LoginManager()
        login_manager.init_app(cls.app)

        @login_manager.user_loader
        def load_user(user_id):
            return db.session.get(User, int(user_id))

        cls.app.register_blueprint(portal_bp)
        cls.app.jinja_loader = ChoiceLoader([
            DictLoader({
                "portal/hr/base.html": "{% block hr_content %}{% endblock %}",
            }),
            cls.app.jinja_loader,
        ])
        cls.app.jinja_env.globals["csrf_token"] = lambda: "test-token"
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

    @staticmethod
    def _pdf_bytes(text: str) -> bytes:
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text((72, 72), text)
            return document.tobytes(garbage=4, deflate=True)
        finally:
            document.close()

    def _authenticated_client(self):
        self.uploader.role = "SUPER_ADMIN"
        db.session.commit()
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(self.uploader.id)
            session["_fresh"] = True
        return client

    def test_bulk_upload_get_renders_current_year_month_and_multi_file_field(self):
        client = self._authenticated_client()

        response = client.get(
            "/portal/hr/payslips/bulk-upload?year=2024&month=11"
        )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('name="pdf_files"', html)
        self.assertIn("multiple", html)
        self.assertIn('<option value="2024" selected>2024</option>', html)
        self.assertIn('<option value="11" selected>11</option>', html)
        self.assertNotIn('name="pdf_file"', html)
        self.assertNotIn('name="files"', html)

    def test_bulk_upload_post_accepts_multiple_pdf_files_and_saves_each_identity(self):
        second_employee = User(
            email="second-employee@example.test",
            name="Second Employee",
            password_hash="x",
            role="employee",
        )
        db.session.add(second_employee)
        db.session.flush()
        db.session.add(EmployeeFile(
            user_id=second_employee.id,
            employee_no="187465",
            full_name_quad="Second Employee",
            national_id="401973847",
        ))
        db.session.commit()

        client = self._authenticated_client()
        form = MultiDict([
            ("year", "2026"),
            ("month", "5"),
            (
                "pdf_files",
                (BytesIO(self._pdf_bytes("Identity 412578742\nRegular employee")), "regular.pdf"),
            ),
            (
                "pdf_files",
                (BytesIO(self._pdf_bytes("Identity 401973847\nDaily wage employee")), "daily.pdf"),
            ),
        ])

        response = client.post(
            "/portal/hr/payslips/bulk-upload",
            data=form,
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 200)
        attachments = (
            EmployeeAttachment.query
            .filter_by(
                attachment_type="PAYSLIP",
                payslip_year=2026,
                payslip_month=5,
            )
            .all()
        )
        self.assertEqual(
            {attachment.user_id for attachment in attachments},
            {self.employee.id, second_employee.id},
        )
        html = response.get_data(as_text=True)
        self.assertIn("412578742", html)
        self.assertIn("401973847", html)
        self.assertIn("regular.pdf", html)
        self.assertIn("daily.pdf", html)

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

    def test_combines_multiple_source_files_before_grouping_by_identity(self):
        work_dir = Path(self.temp_dir.name) / self._testMethodName
        work_dir.mkdir(parents=True, exist_ok=True)
        first_pdf = work_dir / "regular.pdf"
        second_pdf = work_dir / "details.pdf"
        daily_pdf = work_dir / "daily.pdf"

        def write_pdf(path: Path, text: str) -> None:
            document = fitz.open()
            try:
                page = document.new_page()
                page.insert_text((72, 72), text)
                document.save(str(path))
            finally:
                document.close()

        write_pdf(first_pdf, "Identity 412578742\nFirst page")
        write_pdf(second_pdf, "Identity 412578742\nSecond page")
        write_pdf(daily_pdf, "Identity 401973847\nDaily worker page")

        daily_worker = User(
            email="daily-worker@example.test",
            name="Daily Worker",
            password_hash="x",
            role="employee",
        )
        db.session.add(daily_worker)
        db.session.flush()
        db.session.add(EmployeeFile(
            user_id=daily_worker.id,
            employee_no="DW-25",
            full_name_quad="موظف مياومة",
            national_id="401973847",
        ))
        db.session.commit()

        combined_pdf = work_dir / "combined.pdf"
        page_sources = _combine_payslip_pdf_files(
            [
                (first_pdf, "regular.pdf"),
                (second_pdf, "details.pdf"),
                (daily_pdf, "daily.pdf"),
            ],
            combined_pdf,
        )

        with self.app.test_request_context("/"):
            login_user(self.uploader)
            rows, warnings, counters = _split_and_register_payslip_pdf(
                combined_pdf,
                work_dir / "output",
                year=2026,
                month=5,
                source_filenames_by_page=page_sources,
            )
            db.session.commit()

        regular_row = next(row for row in rows if row["identity_number"] == "412578742")
        daily_row = next(row for row in rows if row["identity_number"] == "401973847")
        regular_attachment = EmployeeAttachment.query.filter_by(
            user_id=self.employee.id,
            attachment_type="PAYSLIP",
            payslip_year=2026,
            payslip_month=5,
        ).one()

        self.assertEqual(page_sources, ["regular.pdf", "details.pdf", "daily.pdf"])
        self.assertEqual(counters, {"saved": 2, "skipped": 0, "pages": 3})
        self.assertFalse(warnings)
        self.assertEqual(len(rows), 2)
        self.assertEqual(regular_row["page_numbers"], [1, 2])
        self.assertEqual(regular_row["source_filenames"], ["regular.pdf", "details.pdf"])
        self.assertEqual(daily_row["source_filenames"], ["daily.pdf"])
        self.assertIn("regular.pdf", regular_attachment.note)
        self.assertIn("details.pdf", regular_attachment.note)

        grouped_document = fitz.open(str(work_dir / "output" / regular_row["filename"]))
        try:
            self.assertEqual(grouped_document.page_count, 2)
            self.assertIn("First page", grouped_document[0].get_text())
            self.assertIn("Second page", grouped_document[1].get_text())
        finally:
            grouped_document.close()

    def test_accepts_current_and_legacy_upload_field_names(self):
        current_one = FileStorage(stream=BytesIO(b"one"), filename="one.pdf")
        current_two = FileStorage(stream=BytesIO(b"two"), filename="two.pdf")
        legacy = FileStorage(stream=BytesIO(b"legacy"), filename="legacy.pdf")
        storage = MultiDict([
            ("pdf_files", current_one),
            ("pdf_files", current_two),
            ("files", legacy),
            ("pdf_file", current_one),
        ])

        uploads = _uploaded_payslip_files(storage)

        self.assertEqual(uploads, [current_one, current_two, legacy])

    def test_daily_wage_identity_can_match_legacy_timeclock_record(self):
        daily_worker = User(
            email="daily-worker@example.test",
            name="Daily Worker",
            password_hash="x",
            role="employee",
        )
        db.session.add(daily_worker)
        db.session.flush()
        db.session.add(EmployeeFile(
            user_id=daily_worker.id,
            employee_no="DW-25",
            full_name_quad="موظف مياومة",
            national_id=None,
            timeclock_code="401973847",
        ))
        db.session.commit()

        matched = _find_employee_by_identity("401-973-847")

        self.assertIsNotNone(matched)
        self.assertEqual(matched.user_id, daily_worker.id)

    def test_identity_extraction_accepts_labeled_spaced_digits(self):
        self.assertEqual(
            _extract_identity_number("National ID: 4 0 1-9 7 3 8 4 7"),
            "401973847",
        )

    def test_scanned_daily_wage_page_uses_bundled_rapid_ocr(self):
        class ScannedPage:
            rect = SimpleNamespace(x0=0, y0=0, x1=595, height=842)

            @staticmethod
            def get_text(_mode):
                return ""

            @staticmethod
            def get_pixmap(**_kwargs):
                return SimpleNamespace(tobytes=lambda _format: b"png")

        with self.app.test_request_context("/"):
            with patch(
                "portal.payslips_bulk._run_rapid_ocr_png",
                return_value="Payroll voucher 401973847",
            ), patch(
                "portal.payslips_bulk._resolve_tesseract_command",
                return_value=None,
            ):
                text, used_ocr, warning = _payslip_page_text(ScannedPage())

        self.assertTrue(used_ocr)
        self.assertIsNone(warning)
        self.assertEqual(_extract_identity_number(text), "401973847")

    def test_nonempty_native_text_without_identity_still_uses_ocr(self):
        class PartiallyScannedPage:
            rect = SimpleNamespace(x0=0, y0=0, x1=595, height=842)

            @staticmethod
            def get_text(_mode):
                return "Ministry of Finance payroll"

            @staticmethod
            def get_pixmap(**_kwargs):
                return SimpleNamespace(tobytes=lambda _format: b"png")

        with self.app.test_request_context("/"):
            with patch(
                "portal.payslips_bulk._run_rapid_ocr_png",
                return_value="Identity 401973847",
            ), patch(
                "portal.payslips_bulk._resolve_tesseract_command",
                return_value=None,
            ):
                text, used_ocr, warning = _payslip_page_text(PartiallyScannedPage())

        self.assertTrue(used_ocr)
        self.assertIsNone(warning)
        self.assertIn("Ministry of Finance payroll", text)
        self.assertEqual(_extract_identity_number(text), "401973847")

    def test_tesseract_is_fallback_when_rapid_ocr_cannot_read_identity(self):
        class ScannedPage:
            rect = SimpleNamespace(x0=0, y0=0, x1=595, height=842)

            @staticmethod
            def get_text(_mode):
                return ""

            @staticmethod
            def get_pixmap(**_kwargs):
                return SimpleNamespace(tobytes=lambda _format: b"png")

        with self.app.test_request_context("/"):
            with patch(
                "portal.payslips_bulk._run_rapid_ocr_png",
                return_value="unreadable page",
            ), patch(
                "portal.payslips_bulk._resolve_tesseract_command",
                return_value="tesseract",
            ), patch(
                "portal.payslips_bulk._run_tesseract_png",
                return_value="Identity 401973847",
            ) as tesseract:
                text, used_ocr, warning = _payslip_page_text(ScannedPage())

        self.assertTrue(used_ocr)
        self.assertIsNone(warning)
        self.assertEqual(_extract_identity_number(text), "401973847")
        tesseract.assert_called_once()

    def test_missing_local_ocr_engines_returns_actionable_warning(self):
        class ScannedPage:
            rect = SimpleNamespace(x0=0, y0=0, x1=595, height=842)

            @staticmethod
            def get_text(_mode):
                return "Visible text without an identity number"

            @staticmethod
            def get_pixmap(**_kwargs):
                return SimpleNamespace(tobytes=lambda _format: b"png")

        with self.app.test_request_context("/"):
            with patch(
                "portal.payslips_bulk._run_rapid_ocr_png",
                side_effect=ModuleNotFoundError("rapidocr"),
            ), patch(
                "portal.payslips_bulk._resolve_tesseract_command",
                return_value=None,
            ):
                text, used_ocr, warning = _payslip_page_text(ScannedPage())

        self.assertFalse(used_ocr)
        self.assertEqual(text, "Visible text without an identity number")
        self.assertIn("OCR", warning)


if __name__ == "__main__":
    unittest.main()
