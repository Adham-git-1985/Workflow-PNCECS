import zipfile
from io import BytesIO

import fitz
from docx import Document

from services.official_request_forms import (
    build_leave_request_docx,
    build_leave_request_pdf,
    build_supply_request_docx,
    build_supply_request_pdf,
    build_permission_request_docx,
    build_permission_request_pdf,
    LEAVE_TEMPLATE,
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
    # The filled official page is anchored so Word cannot reflow Arabic fields.
    assert document.element.xpath('//wp:anchor[@behindDoc="1"]')


def test_supply_request_generates_printable_pdf_and_word_form():
    _assert_valid_pdf(build_supply_request_pdf(SUPPLY_DATA), "42")
    _assert_valid_docx(build_supply_request_docx(SUPPLY_DATA))


def test_leave_request_generates_printable_pdf_and_word_form():
    _assert_valid_pdf(build_leave_request_pdf(LEAVE_DATA), "25001")
    _assert_valid_docx(build_leave_request_docx(LEAVE_DATA))


def test_leave_preserves_original_letterhead_title_and_places_employee_number_in_blank():
    with fitz.open(LEAVE_TEMPLATE) as original, fitz.open(stream=build_leave_request_pdf(LEAVE_DATA), filetype='pdf') as filled:
        for box in [(0, 0, 595, 190), (94, 241, 474, 283)]:
            assert original[0].get_pixmap(clip=fitz.Rect(box)).samples == filled[0].get_pixmap(clip=fitz.Rect(box)).samples
        words = [w for w in filled[0].get_text('words') if w[4] == '25001']
        assert len(words) == 1
        assert fitz.Rect(335, 196, 466, 216).contains(fitz.Rect(words[0][:4]))


def test_supply_keeps_all_items_across_continuation_pages():
    data = dict(SUPPLY_DATA, lines=[dict(item=f'UniqueItem{i:02d}', unit='each', quantity=i) for i in range(19)])
    with fitz.open(stream=build_supply_request_pdf(data), filetype='pdf') as pdf:
        assert len(pdf) == 3
        text = ''.join(page.get_text() for page in pdf)
        for i in range(19):
            assert text.count(f'UniqueItem{i:02d}') == 1
        assert '0' in pdf[0].get_text()
    with zipfile.ZipFile(BytesIO(build_supply_request_docx(data))) as archive:
        media = [name for name in archive.namelist() if name.startswith('word/media/')]
        assert len(media) == 3


def test_long_reason_is_carried_in_full_to_an_attachment():
    reason = ' '.join(f'Reason{i:03d}' for i in range(100))
    data = dict(LEAVE_DATA, reason=reason)
    with fitz.open(stream=build_leave_request_pdf(data), filetype='pdf') as pdf:
        assert len(pdf) > 1
        text = ''.join(page.get_text() for page in pdf)
        for token in reason.split():
            assert text.count(token) == 1


def test_permission_generates_reference_form_with_editable_word_values():
    data = dict(employee_no='25001', employee_name='Employee', request_date='2026/09/12',
                day='Saturday', from_time='14:00', to_time='15:00', department='HR')
    _assert_valid_pdf(build_permission_request_pdf(data), '25001')
    _assert_valid_docx(build_permission_request_docx(data))
