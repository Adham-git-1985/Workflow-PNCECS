"""Printable Arabic Word/PDF forms for employee supply and leave requests.

The PDF versions keep the supplied official forms as their background.  The
system only writes the approved request values in their allocated spaces;
signatures are intentionally left as physical-signature lines.
"""

from __future__ import annotations

import os
import unicodedata
from datetime import date
from io import BytesIO
from pathlib import Path

import fitz
from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4, letter

from utils.corr_stamps import _shape_arabic


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
FORM_TEMPLATE_DIR = PROJECT_ROOT / "assets" / "templates" / "forms"
SUPPLY_TEMPLATE = FORM_TEMPLATE_DIR / "supply_request_template.pdf"
LEAVE_TEMPLATE = FORM_TEMPLATE_DIR / "leave_request_template.pdf"
WINDOWS_FONT_DIR = Path(os.environ.get("WINDIR", r"C:\\Windows")) / "Fonts"
FONT_DIR = PROJECT_ROOT / "assets" / "fonts"
REGULAR_FONT = "OfficialFormsSakkal"
BOLD_FONT = "OfficialFormsSakkalBold"
FONT_NAME = "Sakkal Majalla"


def _plain(value, default: str = "-") -> str:
    value = str(value or "").strip()
    return value or default


def _shape(value) -> str:
    raw = unicodedata.normalize("NFC", _plain(value))
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        return get_display(arabic_reshaper.reshape(raw))
    except Exception:
        return _shape_arabic(raw)


def _register_fonts() -> tuple[str, str]:
    registered = set(pdfmetrics.getRegisteredFontNames())
    regular_path = WINDOWS_FONT_DIR / "majalla.ttf"
    bold_path = WINDOWS_FONT_DIR / "majallab.ttf"
    if not regular_path.is_file() or not bold_path.is_file():
        regular_path = FONT_DIR / "DejaVuSans.ttf"
        bold_path = FONT_DIR / "DejaVuSans-Bold.ttf"
    if REGULAR_FONT not in registered:
        pdfmetrics.registerFont(TTFont(REGULAR_FONT, str(regular_path)))
    if BOLD_FONT not in registered:
        pdfmetrics.registerFont(TTFont(BOLD_FONT, str(bold_path)))
    return REGULAR_FONT, BOLD_FONT


def _draw_right(pdf: canvas.Canvas, text, right: float, y: float, *, size: float = 10, bold: bool = False):
    regular, bold_font = _register_fonts()
    pdf.setFont(bold_font if bold else regular, size)
    pdf.drawRightString(right, y, _shape(text))


def _draw_center(pdf: canvas.Canvas, text, center: float, y: float, *, size: float = 10, bold: bool = False):
    regular, bold_font = _register_fonts()
    pdf.setFont(bold_font if bold else regular, size)
    pdf.drawCentredString(center, y, _shape(text))


def _merge_template(template_path: Path, overlay: bytes) -> bytes:
    source = fitz.open(str(template_path))
    layer = fitz.open(stream=overlay, filetype="pdf")
    try:
        source[0].show_pdf_page(source[0].rect, layer, 0, overlay=True)
        return source.tobytes(garbage=4, deflate=True)
    finally:
        layer.close()
        source.close()


def _overlay(page_size) -> tuple[canvas.Canvas, BytesIO]:
    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=page_size)
    pdf.setFillColorRGB(0, 0, 0)
    return pdf, output


def _format_date(value) -> str:
    if isinstance(value, date):
        return value.strftime("%Y/%m/%d")
    return _plain(value, "")


def build_supply_request_pdf(data: dict) -> bytes:
    """Fill the supplied ``نموذج طلب لوازم من المستودع`` PDF."""
    if not SUPPLY_TEMPLATE.is_file():
        raise FileNotFoundError(f"Supply request template is missing: {SUPPLY_TEMPLATE}")
    pdf, output = _overlay(letter)
    _draw_right(pdf, data.get("organization"), 397, 606, size=10)
    _draw_right(pdf, data.get("directorate"), 397, 578, size=10)
    _draw_right(pdf, _format_date(data.get("request_date")), 420, 551, size=10)
    _draw_right(pdf, data.get("request_no"), 275, 524, size=10)

    lines = list(data.get("lines") or [])[:8]
    row_y = [495, 471, 447, 423, 399, 375, 351, 327]
    for index, (line, y) in enumerate(zip(lines, row_y), start=1):
        _draw_center(pdf, index, 522, y, size=9)
        _draw_right(pdf, line.get("item"), 492, y, size=9)
        _draw_center(pdf, line.get("unit"), 245, y, size=9)
        _draw_center(pdf, line.get("quantity"), 145, y, size=9)

    _draw_right(pdf, data.get("requester_name"), 488, 245, size=10)
    _draw_right(pdf, data.get("requester_date"), 488, 194, size=10)
    _draw_right(pdf, data.get("manager_name"), 280, 245, size=10)
    _draw_right(pdf, data.get("warehouse_note"), 500, 112, size=9)
    _draw_right(pdf, data.get("manager_note"), 500, 94, size=9)
    pdf.save()
    return _merge_template(SUPPLY_TEMPLATE, output.getvalue())


def build_leave_request_pdf(data: dict) -> bytes:
    """Fill the supplied ``اجازات`` form while preserving physical signatures."""
    if not LEAVE_TEMPLATE.is_file():
        raise FileNotFoundError(f"Leave request template is missing: {LEAVE_TEMPLATE}")
    pdf, output = _overlay(A4)
    # The reference title has a fixed internal/external label. Cover it only
    # where the data differs, keeping the original border and letterhead.
    pdf.setFillColorRGB(1, 1, 1)
    # Cover the complete title box before replacing its internal/external label.
    pdf.rect(90, 555, 390, 42, stroke=0, fill=1)
    pdf.setFillColorRGB(0, 0, 0)
    _draw_center(pdf, data.get("form_title"), 297, 571, size=17, bold=True)
    _draw_right(pdf, data.get("employee_no"), 473, 639, size=10)
    _draw_right(pdf, _format_date(data.get("request_date")), 470, 611, size=10)
    _draw_right(pdf, data.get("employee_name"), 430, 550, size=10)
    _draw_right(pdf, data.get("job_title"), 265, 550, size=10)
    _draw_right(pdf, _format_date(data.get("start_date")), 460, 528, size=10)
    _draw_right(pdf, data.get("days"), 270, 528, size=10)
    _draw_right(pdf, _format_date(data.get("request_date")), 270, 507, size=10)
    _draw_right(pdf, data.get("reason"), 455, 507, size=10)
    _draw_right(pdf, data.get("entitlement"), 455, 441, size=10)
    _draw_right(pdf, data.get("used"), 310, 421, size=10)
    _draw_right(pdf, data.get("remaining"), 108, 421, size=10)
    _draw_right(pdf, data.get("manager_decision"), 465, 325, size=10)
    _draw_right(pdf, data.get("manager_days"), 465, 305, size=10)
    _draw_right(pdf, data.get("covering_employee"), 465, 283, size=10)
    _draw_right(pdf, data.get("manager_name"), 465, 261, size=10)
    _draw_right(pdf, data.get("secretary_decision"), 210, 325, size=10)
    _draw_right(pdf, data.get("secretary_name"), 210, 261, size=10)
    _draw_right(pdf, data.get("leave_day"), 464, 216, size=10)
    _draw_right(pdf, _format_date(data.get("start_date")), 300, 216, size=10)
    _draw_right(pdf, data.get("return_day"), 464, 195, size=10)
    _draw_right(pdf, _format_date(data.get("return_date")), 300, 195, size=10)
    _draw_right(pdf, data.get("employee_name"), 470, 175, size=9)
    _draw_right(pdf, data.get("manager_name"), 300, 175, size=9)
    _draw_right(pdf, data.get("secretary_name"), 120, 175, size=9)
    pdf.save()
    return _merge_template(LEAVE_TEMPLATE, output.getvalue())


def _set_run_font(run, *, size: float = 12, bold: bool = False):
    run.font.name = FONT_NAME
    run.font.size = Pt(size)
    run.font.bold = bold
    properties = run._element.get_or_add_rPr()
    fonts = properties.get_or_add_rFonts()
    for attr in ("ascii", "hAnsi", "eastAsia", "cs"):
        fonts.set(qn(f"w:{attr}"), FONT_NAME)
    rtl = properties.find(qn("w:rtl"))
    if rtl is None:
        rtl = OxmlElement("w:rtl")
        properties.append(rtl)
    rtl.set(qn("w:val"), "1")


def _set_rtl(paragraph, *, center: bool = False, size: float = 12, bold: bool = False):
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER if center else WD_ALIGN_PARAGRAPH.RIGHT
    paragraph.paragraph_format.space_after = Pt(1)
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.line_spacing = 1
    properties = paragraph._p.get_or_add_pPr()
    bidi = properties.find(qn("w:bidi"))
    if bidi is None:
        bidi = OxmlElement("w:bidi")
        properties.append(bidi)
    bidi.set(qn("w:val"), "1")
    for run in paragraph.runs:
        _set_run_font(run, size=size, bold=bold)


def _set_table_rtl(table):
    table.alignment = WD_TABLE_ALIGNMENT.RIGHT
    props = table._tbl.tblPr
    bidi = props.find(qn("w:bidiVisual"))
    if bidi is None:
        bidi = OxmlElement("w:bidiVisual")
        props.append(bidi)
    bidi.set(qn("w:val"), "1")


def _set_cell(cell, text, *, bold: bool = False, size: float = 10.5, shade: str | None = None):
    cell.text = _plain(text, "")
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    for paragraph in cell.paragraphs:
        _set_rtl(paragraph, center=True, size=size, bold=bold)
    if shade:
        props = cell._tc.get_or_add_tcPr()
        element = OxmlElement("w:shd")
        element.set(qn("w:fill"), shade)
        props.append(element)


def _header_image(template_path: Path, *, height_ratio: float) -> BytesIO | None:
    if not template_path.is_file():
        return None
    document = fitz.open(str(template_path))
    try:
        page = document[0]
        clip = fitz.Rect(0, 0, page.rect.width, page.rect.height * height_ratio)
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip, alpha=False)
        return BytesIO(pix.tobytes("png"))
    finally:
        document.close()


def _new_document(template_path: Path, *, landscape: bool = False, header_ratio: float = 0.23) -> Document:
    doc = Document()
    section = doc.sections[0]
    if landscape:
        section.orientation = WD_ORIENT.LANDSCAPE
        section.page_width, section.page_height = section.page_height, section.page_width
    section.top_margin = Mm(43)
    section.bottom_margin = Mm(13)
    section.left_margin = Mm(14)
    section.right_margin = Mm(14)
    section.header_distance = Mm(4)
    section.footer_distance = Mm(5)
    header_image = _header_image(template_path, height_ratio=header_ratio)
    if header_image:
        paragraph = section.header.paragraphs[0]
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.add_run().add_picture(header_image, width=Mm(178 if not landscape else 260))
    style = doc.styles["Normal"]
    style.font.name = FONT_NAME
    style.font.size = Pt(12)
    return doc


def _add_heading(doc: Document, text: str, *, size: float = 16):
    paragraph = doc.add_paragraph()
    paragraph.add_run(text)
    _set_rtl(paragraph, center=True, size=size, bold=True)
    return paragraph


def _add_key_values(doc: Document, rows: list[tuple[str, str]], *, columns: int = 2):
    table = doc.add_table(rows=0, cols=columns * 2)
    table.style = "Table Grid"
    _set_table_rtl(table)
    for start in range(0, len(rows), columns):
        cells = table.add_row().cells
        for col, (label, value) in enumerate(rows[start:start + columns]):
            # Word visual RTL means the value appears next to its label.
            _set_cell(cells[col * 2], label, bold=True, size=10, shade="F1F1F1")
            _set_cell(cells[col * 2 + 1], value, size=10)
    return table


def _add_signature_line(doc: Document, label: str, name: str):
    paragraph = doc.add_paragraph()
    paragraph.add_run(f"{label}: {_plain(name, '________________')}    التوقيع: __________________")
    _set_rtl(paragraph, size=11, bold=True)


def _docx_bytes(doc: Document, title: str) -> bytes:
    doc.core_properties.title = title
    doc.core_properties.author = "نظام مسار"
    output = BytesIO()
    doc.save(output)
    return output.getvalue()


def build_supply_request_docx(data: dict) -> bytes:
    doc = _new_document(SUPPLY_TEMPLATE, header_ratio=0.26)
    _add_heading(doc, "نموذج طلب لوازم من المستودع")
    _add_key_values(doc, [
        ("الجهة الطالبة / الإدارة العامة", data.get("organization")),
        ("الدائرة", data.get("directorate")),
        ("رقم الطلب", data.get("request_no")),
        ("التاريخ", _format_date(data.get("request_date"))),
    ])
    table = doc.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    _set_table_rtl(table)
    for cell, label in zip(table.rows[0].cells, ("الرقم", "الصنف", "الوحدة", "الكمية المطلوبة")):
        _set_cell(cell, label, bold=True, shade="E9E9E9")
    for index, line in enumerate(data.get("lines") or [], start=1):
        cells = table.add_row().cells
        _set_cell(cells[0], index)
        _set_cell(cells[1], line.get("item"))
        _set_cell(cells[2], line.get("unit"))
        _set_cell(cells[3], line.get("quantity"))
    doc.add_paragraph()
    _add_signature_line(doc, "اسم المستلم", data.get("requester_name"))
    _add_signature_line(doc, "المسؤول المباشر", data.get("manager_name"))
    _add_signature_line(doc, "مدير المستودع", data.get("warehouse_name"))
    for label, value in (("ملاحظة المسؤول المباشر", data.get("manager_note")), ("ملاحظة مدير المستودع", data.get("warehouse_note"))):
        paragraph = doc.add_paragraph()
        paragraph.add_run(f"{label}: {_plain(value, '_______________________________')}")
        _set_rtl(paragraph, size=11)
    return _docx_bytes(doc, "نموذج طلب لوازم من المستودع")


def build_leave_request_docx(data: dict) -> bytes:
    doc = _new_document(LEAVE_TEMPLATE, header_ratio=0.20)
    _add_heading(doc, data.get("form_title") or "طلب إجازة")
    _add_key_values(doc, [
        ("رقم الموظف", data.get("employee_no")),
        ("تاريخ تقديم الطلب", _format_date(data.get("request_date"))),
        ("الاسم", data.get("employee_name")),
        ("الوظيفة", data.get("job_title")),
        ("من تاريخ", _format_date(data.get("start_date"))),
        ("إلى تاريخ", _format_date(data.get("end_date"))),
        ("مدة الإجازة", data.get("days")),
        ("سبب الإجازة", data.get("reason")),
    ])
    _add_heading(doc, "لاستعمال وحدة شؤون الموظفين بالوزارة", size=14)
    _add_key_values(doc, [
        ("مقدار الإجازة المستحقة", data.get("entitlement")),
        ("استنفذ منها", data.get("used")),
        ("الرصيد المتبقي", data.get("remaining")),
        ("نوع الإجازة", data.get("leave_type")),
    ])
    _add_heading(doc, "لاستعمال المسؤول المباشر", size=14)
    _add_key_values(doc, [
        ("القرار", data.get("manager_decision")),
        ("مدة الإجازة المصادق عليها", data.get("manager_days")),
        ("من يقوم بالعمل مكانه", data.get("covering_employee")),
    ], columns=1)
    _add_signature_line(doc, "المسؤول المباشر", data.get("manager_name"))
    _add_heading(doc, "لاستعمال الوزير / الوكيل / الإدارة العامة", size=14)
    _add_key_values(doc, [("القرار", data.get("secretary_decision"))], columns=1)
    _add_signature_line(doc, "الأمين العام", data.get("secretary_name"))
    _add_heading(doc, "إقرار القيام بالإجازة", size=14)
    _add_key_values(doc, [
        ("يوم وتاريخ القيام بالإجازة", f"{_plain(data.get('leave_day'), '')} {_format_date(data.get('start_date'))}"),
        ("يوم وتاريخ العودة من الإجازة", f"{_plain(data.get('return_day'), '')} {_format_date(data.get('return_date'))}"),
    ], columns=1)
    _add_signature_line(doc, "الموظف", data.get("employee_name"))
    _add_signature_line(doc, "المسؤول المباشر", data.get("manager_name"))
    _add_signature_line(doc, "الأمين العام", data.get("secretary_name"))
    return _docx_bytes(doc, "طلب إجازة")
