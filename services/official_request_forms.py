"""Official source artwork with bounded, editable RTL fields in PDF and Word.

Coordinates are top-origin PDF points. Both exporters consume the same layout;
only placeholder ink inside explicitly allocated value boxes is covered.
"""
from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from datetime import date
from io import BytesIO
from pathlib import Path

import arabic_reshaper
import fitz
from bidi.algorithm import get_display
from docx import Document
from docx.oxml import parse_xml
from docx.shared import Pt
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
FORM_TEMPLATE_DIR = PROJECT_ROOT / "assets" / "templates" / "forms"
LEAVE_TEMPLATE = FORM_TEMPLATE_DIR / "leave_request_template.pdf"
SUPPLY_TEMPLATE = FORM_TEMPLATE_DIR / "supply_request_template.pdf"
PERMISSION_TEMPLATE = FORM_TEMPLATE_DIR / "permission_request_template.png"
FONT_NAME = "Sakkal Majalla"
REGULAR_FONT = "OfficialFormsSakkal"
_RESHAPER = arabic_reshaper.ArabicReshaper(configuration={"support_ligatures": False})


def _plain(value, default=""):
    return str(value if value is not None else "").strip() or default


def _format_date(value):
    return value.strftime("%Y/%m/%d") if isinstance(value, date) else _plain(value).replace("-", "/")


def _shape(value):
    return get_display(_RESHAPER.reshape(unicodedata.normalize("NFC", _plain(value))))


def _register_fonts():
    if REGULAR_FONT not in pdfmetrics.getRegisteredFontNames():
        path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "majalla.ttf"
        if not path.is_file():
            path = PROJECT_ROOT / "assets/fonts/DejaVuSans.ttf"
        pdfmetrics.registerFont(TTFont(REGULAR_FONT, str(path)))


@dataclass(frozen=True)
class Field:
    key: str
    box: tuple[float, float, float, float]
    text: str
    size: float = 12
    center: bool = False
    clear: bool = True


def _field(data, key, box, **kwargs):
    value = data.get(key)
    if "date" in key:
        value = _format_date(value)
    return Field(key, box, _plain(value), **kwargs)


def _leave_fields(data):
    # All boxes exclude printed labels, title borders and signature rules.
    specs = {
        "employee_no": (335, 198, 466, 214), "request_date": (350, 218, 466, 235),
        "employee_name": (317, 285, 480, 300), "job_title": (75, 285, 200, 300),
        "start_date": (317, 305, 400, 322), "days": (75, 305, 190, 322),
        "reason": (317, 327, 443, 342),
        "entitlement": (409, 414, 521, 429), "used": (263, 414, 309, 429),
        "remaining": (65, 414, 128, 429),
        "manager_decision": (308, 506, 418, 522), "manager_days": (308, 527, 417, 543),
        "covering_employee": (308, 547, 423, 563),
        "secretary_decision": (57, 506, 168, 522),
        "leave_day": (300, 617, 366, 632), "return_day": (300, 638, 366, 653),
        "return_date": (158, 638, 260, 653),
    }
    fields = [_field(data, key, box) for key, box in specs.items()]
    fields += [_field(data, "request_date", (72, 327, 192, 342)),
               _field(data, "start_date", (158, 617, 260, 632))]
    # Names above the dotted signature line leave the line itself for handwriting.
    for key, box in [("manager_name", (309, 562, 428, 574)),
                     ("secretary_name", (51, 562, 190, 574)),
                     ("employee_name", (381, 671, 520, 682)),
                     ("manager_name", (228, 671, 363, 682)),
                     ("secretary_name", (54, 671, 206, 682))]:
        fields.append(_field(data, key, box, size=10, center=True, clear=False))
    # Keep original title and its mixed fonts; specify type within the reason field.
    return fields


def _supply_pages(data):
    lines = list(data.get("lines") or [])
    pages = []
    for start in range(0, max(1, len(lines)), 8):
        fields = [_field(data, key, box) for key, box in {
            "organization": (91, 178, 395, 197), "directorate": (91, 206, 395, 225),
            "request_date": (396, 232, 476, 247), "request_no": (96, 252, 123, 267),
            "requester_name": (324, 535, 473, 551), "manager_name": (99, 535, 229, 551),
            "requester_date": (324, 589, 495, 608),
            "warehouse_note": (91, 654, 532, 672), "manager_note": (91, 677, 532, 694),
        }.items()]
        for index, line in enumerate(lines[start:start + 8]):
            top = 298 + index * 23.7
            values = dict(line, number=start + index + 1)
            for key, left, right in [("number", 504, 539), ("item", 298, 498),
                                     ("unit", 206, 292), ("quantity", 93, 200)]:
                fields.append(_field(values, key, (left, top, right, top + 20),
                                     size=11, center=key != "item", clear=False))
        if data.get("warehouse_name"):
            fields.append(Field("warehouse_name", (91, 706, 530, 722),
                                "مدير المستودع: " + _plain(data["warehouse_name"]), size=11, clear=False))
        if len(lines) > 8:
            fields.append(Field("page", (280, 738, 340, 750),
                                f"{start // 8 + 1} / {(len(lines) + 7) // 8}", size=9, center=True, clear=False))
        pages.append(fields)
    return pages


def _permission_fields(data):
    # Supplied scan is 995 x 620, placed at 0.6 pt per pixel (597 x 372 pt).
    boxes = {"day": (429, 118, 490, 139), "request_date": (291, 118, 379, 140),
             "employee_name": (343, 158, 494, 176), "department": (219, 158, 299, 176),
             "employee_no": (79, 158, 135, 176), "destination": (461, 220, 527, 238),
             "from_time": (375, 220, 449, 238), "to_time": (271, 220, 337, 238),
             "manager_name": (402, 286, 529, 303), "director_name": (82, 286, 205, 303)}
    return [_field(data, key, box, size=12, center=True,
                   clear=key not in ("manager_name", "director_name")) for key, box in boxes.items()]


def _source(template):
    if template.suffix.lower() == ".pdf":
        return fitz.open(str(template))
    source = fitz.open()
    page = source.new_page(width=597, height=372)
    page.insert_image(page.rect, filename=str(template))
    return source


def _background(source, fields):
    result = fitz.open()
    result.insert_pdf(source, from_page=0, to_page=0)
    for field in fields:
        if field.clear and field.text:
            result[0].draw_rect(fitz.Rect(field.box), color=None, fill=(1, 1, 1), overlay=True)
    return result


def _fit(field):
    """Wrap and shrink to fit, never let a long value cover neighboring labels."""
    _register_fonts()
    width = field.box[2] - field.box[0] - 4
    height = field.box[3] - field.box[1]
    for size in [field.size - n * .5 for n in range(int((field.size - 7) * 2) + 1)]:
        lines = []
        current = ""
        for word in field.text.split():
            candidate = (current + " " + word).strip()
            if current and pdfmetrics.stringWidth(_shape(candidate), REGULAR_FONT, size) > width:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
        if len(lines) * size <= height and all(pdfmetrics.stringWidth(_shape(line), REGULAR_FONT, size) <= width for line in lines):
            return size, lines
    # Overflow is explicitly carried onto a continuation page, never truncated.
    return None, []


def _prepared_pages(pages, width, height):
    prepared = []
    overflow = []
    for fields in pages:
        fitted = []
        for field in fields:
            if not field.text:
                continue
            size, lines = _fit(field)
            if size is None:
                overflow.append(field)
                field = Field(field.key, field.box, f"({len(overflow)}) انظر المرفق", 9, field.center, field.clear)
            fitted.append(field)
        prepared.append(fitted)
    # Continuations use readable full-width text, while the official first pages stay unchanged.
    if overflow:
        title = Field("continuation", (40, 25, width - 40, 45),
                      "مرفق النموذج - استكمال البيانات", 14, center=True, clear=False)
        continuation = [title]
        top = 65
        for number, field in enumerate(overflow, 1):
            words = field.text.split()
            while words:
                chunk = []
                while words and len(" ".join(chunk)) < 80:
                    word = words.pop(0)
                    if len(word) > 60:
                        chunk.append(word[:60])
                        words.insert(0, word[60:])
                        break
                    chunk.append(word)
                if top + 45 > height - 45:
                    prepared.append(continuation)
                    continuation, top = [title], 65
                continuation.append(Field("continuation", (40, top, width - 40, top + 40),
                                          f"({number}) " + " ".join(chunk), 12, clear=False))
                top += 42
        prepared.append(continuation)
    return prepared


def _build_pdf(template, pages):
    with _source(template) as source:
        width, height = source[0].rect.width, source[0].rect.height
        pages = _prepared_pages(pages, width, height)
        output = fitz.open()
        for fields in pages:
            continuation = fields and fields[0].key == "continuation"
            if continuation:
                output.new_page(width=width, height=height)
            else:
                with _background(source, fields) as background:
                    output.insert_pdf(background)
            stream = BytesIO()
            pdf = canvas.Canvas(stream, pagesize=(width, height))
            for field in fields:
                size, lines = _fit(field)
                if size is None:
                    raise ValueError(f"Official form field cannot fit: {field.key}")
                left, top, right, bottom = field.box
                pdf.setFont(REGULAR_FONT, size)
                baseline = height - top - (bottom - top - len(lines) * size) / 2 - size * .8
                for line in lines:
                    if field.center:
                        pdf.drawCentredString((left + right) / 2, baseline, _shape(line))
                    else:
                        pdf.drawRightString(right - 2, baseline, _shape(line))
                    baseline -= size
            pdf.save()
            with fitz.open(stream=stream.getvalue(), filetype="pdf") as layer:
                output[-1].show_pdf_page(output[-1].rect, layer, 0)
        result = output.tobytes(garbage=4, deflate=True)
        output.close()
        return result


def _build_docx(template, pages):
    """Put the final official pages in Word without letting Word reflow them.

    Word and LibreOffice shape Arabic and positioned text differently. Embedding
    the already-filled page preserves every supplied font, border and RTL
    position and keeps Word useful for printing and paper signatures.
    """
    rendered = _build_pdf(template, pages)
    doc = Document()
    with fitz.open(stream=rendered, filetype="pdf") as source:
        width, height = source[0].rect.width, source[0].rect.height
        section = doc.sections[0]
        section.page_width, section.page_height = Pt(width), Pt(height)
        section.top_margin = section.bottom_margin = Pt(0)
        section.left_margin = section.right_margin = Pt(0)
        section.header_distance = section.footer_distance = Pt(0)
        for page_index, page in enumerate(source):
            paragraph = doc.add_paragraph()
            paragraph.paragraph_format.space_after = Pt(0)
            paragraph.paragraph_format.line_spacing = Pt(1)
            paragraph.paragraph_format.page_break_before = page_index > 0
            png = page.get_pixmap(matrix=fitz.Matrix(3, 3), alpha=False).tobytes("png")
            inline = paragraph.add_run().add_picture(BytesIO(png), width=Pt(width), height=Pt(height))._inline
            # Anchor each rendered page to its physical page, outside text flow.
            anchor = parse_xml('''<wp:anchor xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
              distT="0" distB="0" distL="0" distR="0" simplePos="0" relativeHeight="0" behindDoc="1" locked="1" layoutInCell="1" allowOverlap="1">
              <wp:simplePos x="0" y="0"/><wp:positionH relativeFrom="page"><wp:posOffset>0</wp:posOffset></wp:positionH>
              <wp:positionV relativeFrom="page"><wp:posOffset>0</wp:posOffset></wp:positionV><wp:wrapNone/></wp:anchor>''')
            for child in list(inline):
                anchor.append(child)
            inline.getparent().replace(inline, anchor)
    doc.core_properties.title = "نموذج رسمي"
    doc.core_properties.author = "نظام مسار"
    stream = BytesIO()
    doc.save(stream)
    return stream.getvalue()


def build_leave_request_pdf(data):
    return _build_pdf(LEAVE_TEMPLATE, [_leave_fields(data)])


def build_leave_request_docx(data):
    return _build_docx(LEAVE_TEMPLATE, [_leave_fields(data)])


def build_supply_request_pdf(data):
    return _build_pdf(SUPPLY_TEMPLATE, _supply_pages(data))


def build_supply_request_docx(data):
    return _build_docx(SUPPLY_TEMPLATE, _supply_pages(data))


def build_permission_request_pdf(data):
    return _build_pdf(PERMISSION_TEMPLATE, [_permission_fields(data)])


def build_permission_request_docx(data):
    return _build_docx(PERMISSION_TEMPLATE, [_permission_fields(data)])
