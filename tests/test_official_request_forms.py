import zipfile
from io import BytesIO

import fitz
from docx import Document

from services.official_request_forms import (
    build_leave_request_docx,
    build_leave_request_pdf,
    build_supply_request_docx,
    build_supply_request_pdf,
)


SUPPLY_DATA = {
    "organization": "Palestinian National Commission",
    "directorate": "Administrative Affairs",
    "request_no": "42",
    "request_date": "2026/09/12",
    "requester_name": "Requesting Employee",
    "manager_name": "Direct Manager",
    "warehouse_name": "Warehouse Manager",
    "manager_note": "Reviewed",
    "warehouse_note": "Issued from main warehouse",
    "lines": [{"item": "A4 paper", "unit": "ream", "quantity": "5"}],
}

LEAVE_DATA = {
    "form_title": "Internal leave request",
    "employee_no": "25001",
    "request_date": "2026/09/12",
    "employee_name": "Leave Employee",
    "job_title": "Director",
    "start_date": "2026/09/20",
    "end_date": "2026/09/24",
    "days": "5 days",
    "reason": "Annual leave",
    "entitlement": "30 days",
    "used": "10 days",
    "remaining": "20 days",
    "manager_decision": "Approved",
    "manager_days": "5 days",
    "covering_employee": "Covering Employee",
    "manager_name": "Direct Manager",
    "secretary_decision": "Approved",
    "secretary_name": "Secretary General",
    "leave_day": "Sunday",
    "return_day": "Thursday",
    "return_date": "2026/09/25",
}


def _assert_valid_pdf(content: bytes, expected_text: str):
    document = fitz.open(stream=content, filetype="pdf")
    try:
        assert document.page_count == 1
        assert expected_text in document[0].get_text()
    finally:
        document.close()


def _assert_valid_docx(content: bytes):
    with zipfile.ZipFile(BytesIO(content)) as archive:
        names = set(archive.namelist())
    assert "word/document.xml" in names
    assert any(name.startswith("word/media/") for name in names)
    document = Document(BytesIO(content))
    assert document.sections
    assert document.tables


def test_supply_request_generates_printable_pdf_and_word_form():
    _assert_valid_pdf(build_supply_request_pdf(SUPPLY_DATA), "42")
    _assert_valid_docx(build_supply_request_docx(SUPPLY_DATA))


def test_leave_request_generates_printable_pdf_and_word_form():
    _assert_valid_pdf(build_leave_request_pdf(LEAVE_DATA), "25001")
    _assert_valid_docx(build_leave_request_docx(LEAVE_DATA))
