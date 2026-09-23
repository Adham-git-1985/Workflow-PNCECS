import zipfile
from io import BytesIO

import fitz
from docx import Document

from services.official_request_forms import (
    build_attendance_delay_justification_docx,
    build_attendance_delay_notice_docx,
    build_leave_request_docx,
    build_leave_request_pdf,
    build_supply_request_docx,
    build_supply_request_pdf,
    build_permission_request_docx,
    build_permission_request_pdf,
    LEAVE_TEMPLATE,
    official_form_filename,
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
    # The page artwork uses a schema-valid page anchor and cannot be reflowed.
    anchors = document.element.xpath('//wp:anchor')
    assert anchors and not document.element.xpath('//wp:inline')
    children = [node.tag.rsplit('}', 1)[-1] for node in anchors[0]]
    assert children[:4] == ['simplePos', 'positionH', 'positionV', 'extent']
    assert children.index('wrapNone') < children.index('docPr')


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


def test_permission_generates_reference_form_in_pdf_and_word():
    data = dict(employee_no='25001', employee_name='Employee', request_date='2026/09/12',
                day='Saturday', from_time='14:00', to_time='15:00', department='HR')
    _assert_valid_pdf(build_permission_request_pdf(data), '25001')
    _assert_valid_docx(build_permission_request_docx(data))


def test_attendance_delay_forms_are_editable_rtl_word_documents_with_letterhead():
    data = {
        "request_no": 189,
        "request_date": "2026/09/23",
        "initiator_name": "الشؤون البشرية",
        "employee_name": "موظف تجريبي",
        "employee_no": "EMP-3492",
        "job_title": "موظف",
        "department": "دائرة الموارد البشرية",
        "delay_day": "2026/09/23",
        "first_in_time": "09:15",
        "late_minutes": 75,
        "early_leave_minutes": 0,
    }
    blank = dict(data, case_kind="DELAY", has_document="NO")
    completed = dict(blank, reason="ظرف طارئ", has_document="YES", document_name="إفادة رسمية")

    for content, expected in (
        (build_attendance_delay_notice_docx(data), "نموذج تأخير عن العمل"),
        (build_attendance_delay_justification_docx(blank), "نموذج تبرير غياب / تأخير"),
        (build_attendance_delay_justification_docx(completed), "ظرف طارئ"),
    ):
        document = Document(BytesIO(content))
        assert expected in "\n".join(p.text for p in document.paragraphs)
        assert document.sections[0].header.tables
        header_text = "\n".join(p.text for p in document.sections[0].header.paragraphs)
        assert "189" in header_text and "2026/09/23" in header_text
        body_runs = [run for paragraph in document.paragraphs for run in paragraph.runs]
        assert body_runs
        assert all(run.font.name == "Sakkal Majalla" for run in body_runs)
        assert all(run.font.size and run.font.size.pt == 16 for run in body_runs)
        assert all(
            paragraph._p.pPr is not None
            and paragraph._p.pPr.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}bidi") is not None
            for paragraph in document.paragraphs
        )
        with zipfile.ZipFile(BytesIO(content)) as archive:
            assert "word/media/image1.jpeg" in archive.namelist()


def test_official_filename_keeps_arabic_and_removes_windows_reserved_characters():
    assert official_form_filename('أحمد/محمد:علي', 'طلب المواد', 'DOCX') == 'أحمد محمد علي - طلب المواد.docx'
