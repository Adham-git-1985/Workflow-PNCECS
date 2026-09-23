"""Official source artwork with bounded, editable RTL fields in PDF and Word.

Coordinates are top-origin PDF points. Both exporters consume the same layout;
only placeholder ink inside explicitly allocated value boxes is covered.
"""
from __future__ import annotations

import os
import re
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
from docx.oxml.ns import qn
from docx.shared import Pt
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader

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


def official_form_filename(employee_name, form_label, extension):
    """Return an Arabic, Windows-safe download name based on the employee."""
    def clean(value, fallback):
        value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", _plain(value, fallback))
        return re.sub(r"\s+", " ", value).strip(" .") or fallback

    name = clean(employee_name, "موظف")[:120].rstrip()
    label = clean(form_label, "نموذج")[:50].rstrip()
    suffix = re.sub(r"[^a-z0-9]", "", _plain(extension).lower()) or "pdf"
    return f"{name} - {label}.{suffix}"


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
            inline = paragraph.add_run().add_picture(
                BytesIO(png), width=Pt(width), height=Pt(height - 2),
            )._inline
            # Reuse Word's own image nodes and only change their container to a
            # schema-valid page anchor. Child order is significant to Word.
            anchor = parse_xml('''<wp:anchor xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
              distT="0" distB="0" distL="0" distR="0" simplePos="0" relativeHeight="0"
              behindDoc="1" locked="1" layoutInCell="1" allowOverlap="1">
              <wp:simplePos x="0" y="0"/>
              <wp:positionH relativeFrom="page"><wp:posOffset>0</wp:posOffset></wp:positionH>
              <wp:positionV relativeFrom="page"><wp:posOffset>0</wp:posOffset></wp:positionV>
            </wp:anchor>''')
            for tag in ("extent", "effectExtent"):
                child = inline.find(qn(f"wp:{tag}"))
                if child is not None:
                    anchor.append(child)
            anchor.append(parse_xml(
                '<wp:wrapNone xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"/>',
            ))
            for tag in ("docPr", "cNvGraphicFramePr"):
                child = inline.find(qn(f"wp:{tag}"))
                if child is not None:
                    anchor.append(child)
            graphic = inline.find(qn("a:graphic"))
            if graphic is not None:
                anchor.append(graphic)
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


# ======================
# Attendance delay forms
# ======================
ATTENDANCE_DELAY_WORKFLOW_LABEL = "نموذج تأخير عن العمل"
ATTENDANCE_DELAY_RESPONSE_LABEL = "نموذج تبرير غياب / تأخير"


def _attendance_text(value, default=""):
    return _plain(value, default)


def _attendance_draw_rtl(c, value, right, top, width, *, size=12, leading=None, color=colors.black):
    """Draw bounded RTL text using the same shaping as the official forms."""
    text = _attendance_text(value)
    if not text:
        return top
    _register_fonts()
    leading = leading or size * 1.35
    words = text.split()
    lines = []
    current = ""
    for word in words:
        candidate = (current + " " + word).strip()
        if current and pdfmetrics.stringWidth(_shape(candidate), REGULAR_FONT, size) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    c.setFillColor(color)
    c.setFont(REGULAR_FONT, size)
    y = top
    for line in lines:
        c.drawRightString(right, y, _shape(line))
        y -= leading
    return y


def _attendance_field(c, label, value, x, y, width, *, label_width=105, size=11, height=27):
    c.setStrokeColor(colors.HexColor("#6b7280"))
    c.setFillColor(colors.white)
    c.roundRect(x, y - height, width, height, 4, stroke=1, fill=1)
    c.setFillColor(colors.HexColor("#374151"))
    c.setFont(REGULAR_FONT, size)
    c.drawRightString(x + width - 8, y - 18, _shape(_attendance_text(label)))
    value_x = x + 8
    value_width = max(40, width - label_width - 12)
    _attendance_draw_rtl(
        c,
        value,
        x + value_width,
        y - 18,
        value_width - 8,
        size=size,
        color=colors.HexColor("#111827"),
    )


def _attendance_header(c, title, data, width, height):
    _register_fonts()
    logo = PROJECT_ROOT / "static" / "images" / "pncecs_logo.png"
    if logo.is_file():
        c.drawImage(
            ImageReader(str(logo)),
            width - 88,
            height - 88,
            width=58,
            height=58,
            preserveAspectRatio=True,
            mask="auto",
        )
    c.setFillColor(colors.HexColor("#123b63"))
    c.setFont(REGULAR_FONT, 15)
    c.drawCentredString(width / 2, height - 40, _shape("نظام مسار - إدارة الموارد البشرية"))
    c.setFont(REGULAR_FONT, 10)
    c.setFillColor(colors.HexColor("#4b5563"))
    c.drawCentredString(width / 2, height - 58, _shape("نموذج رسمي مولد من النظام"))
    c.setStrokeColor(colors.HexColor("#123b63"))
    c.setLineWidth(1.2)
    c.line(38, height - 98, width - 38, height - 98)
    c.setFillColor(colors.HexColor("#123b63"))
    c.setFont(REGULAR_FONT, 18)
    c.drawCentredString(width / 2, height - 132, _shape(title))
    c.setFont(REGULAR_FONT, 9)
    c.setFillColor(colors.HexColor("#6b7280"))
    request_no = _attendance_text(data.get("request_no"), "-")
    request_date = _format_date(data.get("request_date"))
    c.drawString(42, height - 151, _shape(f"رقم الطلب: {request_no}"))
    c.drawRightString(width - 42, height - 151, _shape(f"تاريخ الإنشاء: {request_date}"))


def _attendance_form_canvas(data, title, draw_body):
    _register_fonts()
    stream = BytesIO()
    width, height = A4
    c = canvas.Canvas(stream, pagesize=A4)
    c.setTitle(title)
    c.setAuthor("Workflow-PNCECS")
    _attendance_header(c, title, data, width, height)
    draw_body(c, data, width, height)
    c.setStrokeColor(colors.HexColor("#d1d5db"))
    c.line(38, 38, width - 38, 38)
    c.setFillColor(colors.HexColor("#6b7280"))
    c.setFont(REGULAR_FONT, 8)
    c.drawCentredString(width / 2, 24, _shape("هذه الوثيقة جزء من مسار إلكتروني موثق ولا تغني عن الاعتماد داخل النظام."))
    c.save()
    return stream.getvalue()


def build_attendance_delay_notice_pdf(data):
    """Create the official, system-filled late-attendance notice."""
    data = dict(data or {})

    def body(c, values, width, height):
        y = height - 185
        _attendance_field(c, "اسم الموظف", values.get("employee_name"), 42, y, width - 84, label_width=105)
        y -= 39
        _attendance_field(c, "الرقم الوظيفي", values.get("employee_no"), 42, y, (width - 95) / 2, label_width=90)
        _attendance_field(c, "المسمى الوظيفي", values.get("job_title"), 53 + (width - 95) / 2, y, (width - 95) / 2, label_width=100)
        y -= 39
        _attendance_field(c, "الإدارة", values.get("department"), 42, y, (width - 95) / 2, label_width=75)
        _attendance_field(c, "يوم التأخير", values.get("delay_day"), 53 + (width - 95) / 2, y, (width - 95) / 2, label_width=85)
        y -= 54

        c.setFillColor(colors.HexColor("#eff6ff"))
        c.setStrokeColor(colors.HexColor("#93c5fd"))
        c.roundRect(42, y - 72, width - 84, 72, 6, stroke=1, fill=1)
        c.setFillColor(colors.HexColor("#1e3a8a"))
        c.setFont(REGULAR_FONT, 13)
        c.drawRightString(width - 58, y - 24, _shape("بيانات الدوام المحتسبة"))
        c.setFillColor(colors.HexColor("#111827"))
        c.setFont(REGULAR_FONT, 11)
        facts = (
            f"وقت الدخول: {_attendance_text(values.get('first_in_time'), '-')}",
            f"التأخير الصباحي: {_attendance_text(values.get('late_minutes'), '0')} دقيقة",
            f"الخروج المبكر: {_attendance_text(values.get('early_leave_minutes'), '0')} دقيقة",
        )
        fact_text = "  |  ".join(facts)
        c.drawCentredString(width / 2, y - 51, _shape(fact_text))
        y -= 105

        c.setFillColor(colors.HexColor("#111827"))
        c.setFont(REGULAR_FONT, 13)
        c.drawRightString(width - 42, y, _shape("الإشعار"))
        c.setStrokeColor(colors.HexColor("#9ca3af"))
        c.roundRect(42, y - 150, width - 84, 132, 6, stroke=1, fill=0)
        notice = (
            "تبين من سجل الدوام الإلكتروني وجود تأخير للموظف الموضح أعلاه في اليوم المحدد. "
            "يرجى الدخول إلى المسار وتعبئة نموذج تبرير غياب / تأخير، وبيان سبب التأخير وإرفاق ما يؤيده إن وجد. "
            "في حال عدم الرد خلال ساعتين من وقت استلام الطلب، قد يتم اتخاذ الإجراءات الإدارية النظامية."
        )
        _attendance_draw_rtl(c, notice, width - 58, y - 45, width - 116, size=12, leading=21)
        y -= 190
        c.setFont(REGULAR_FONT, 11)
        c.drawRightString(width - 58, y, _shape("منشئ الطلب: " + _attendance_text(values.get("initiator_name"), "الشؤون البشرية")))
        c.drawRightString(width - 58, y - 26, _shape("الموظف مطالب بالرد من خلال المسار الإلكتروني."))
        c.setStrokeColor(colors.HexColor("#6b7280"))
        c.line(60, 125, 245, 125)
        c.line(width - 245, 125, width - 60, 125)
        c.setFont(REGULAR_FONT, 10)
        c.drawCentredString(152, 108, _shape("توقيع الموظف عند الحاجة"))
        c.drawCentredString(width - 152, 108, _shape("ختم / توقيع الجهة"))

    return _attendance_form_canvas(data, ATTENDANCE_DELAY_WORKFLOW_LABEL, body)


def build_attendance_delay_justification_pdf(data):
    """Create the blank or completed justification form."""
    data = dict(data or {})
    is_completed = bool(_attendance_text(data.get("reason")))
    title = ATTENDANCE_DELAY_RESPONSE_LABEL

    def body(c, values, width, height):
        y = height - 185
        _attendance_field(c, "اسم الموظف", values.get("employee_name"), 42, y, width - 84, label_width=105)
        y -= 39
        _attendance_field(c, "الرقم الوظيفي", values.get("employee_no"), 42, y, (width - 95) / 2, label_width=90)
        _attendance_field(c, "المسمى الوظيفي", values.get("job_title"), 53 + (width - 95) / 2, y, (width - 95) / 2, label_width=100)
        y -= 39
        _attendance_field(c, "الإدارة", values.get("department"), 42, y, (width - 95) / 2, label_width=75)
        _attendance_field(c, "تاريخ الحالة", values.get("delay_day"), 53 + (width - 95) / 2, y, (width - 95) / 2, label_width=85)
        y -= 58

        c.setFont(REGULAR_FONT, 12)
        c.setFillColor(colors.HexColor("#111827"))
        c.drawRightString(width - 42, y, _shape("نوع الحالة"))
        kind = _attendance_text(values.get("case_kind"), "DELAY").upper()
        for index, (key, label) in enumerate((("ABSENCE", "غياب"), ("DELAY", "تأخير"))):
            x = width - 155 - index * 120
            c.setStrokeColor(colors.HexColor("#374151"))
            c.rect(x, y - 17, 13, 13, stroke=1, fill=0)
            if kind == key:
                c.setFillColor(colors.HexColor("#123b63"))
                c.rect(x + 3, y - 14, 7, 7, stroke=0, fill=1)
            c.setFillColor(colors.HexColor("#111827"))
            c.drawRightString(x - 8, y - 13, _shape(label))
        y -= 48

        c.setFont(REGULAR_FONT, 12)
        c.drawRightString(width - 42, y, _shape("سبب الغياب / التأخير"))
        c.setStrokeColor(colors.HexColor("#9ca3af"))
        c.roundRect(42, y - 112, width - 84, 95, 6, stroke=1, fill=0)
        reason = values.get("reason") or ""
        if reason:
            _attendance_draw_rtl(c, reason, width - 58, y - 42, width - 116, size=12, leading=20)
        else:
            c.setFillColor(colors.HexColor("#9ca3af"))
            c.setFont(REGULAR_FONT, 10)
            c.drawRightString(width - 58, y - 42, _shape("يعبأ من الموظف عبر النظام"))
        y -= 150

        has_doc = _attendance_text(values.get("has_document"), "NO").upper()
        c.setFillColor(colors.HexColor("#111827"))
        c.setFont(REGULAR_FONT, 11)
        c.drawRightString(width - 42, y, _shape("هل يوجد مستند مؤيد؟"))
        c.drawRightString(width - 190, y, _shape("نعم" + (" [X]" if has_doc == "YES" else " [ ]")))
        c.drawRightString(width - 275, y, _shape("لا" + (" [X]" if has_doc != "YES" else " [ ]")))
        y -= 37
        _attendance_field(c, "اسم / مرجع المستند", values.get("document_name") or values.get("document_reference"), 42, y, width - 84, label_width=145, size=10)
        y -= 62

        c.setFillColor(colors.HexColor("#f9fafb"))
        c.setStrokeColor(colors.HexColor("#d1d5db"))
        c.roundRect(42, y - 102, width - 84, 102, 6, stroke=1, fill=1)
        c.setFillColor(colors.HexColor("#374151"))
        c.setFont(REGULAR_FONT, 11)
        c.drawRightString(width - 58, y - 22, _shape("قرارات الاعتماد"))
        c.setFont(REGULAR_FONT, 10)
        c.drawRightString(width - 58, y - 48, _shape("المدير المباشر: " + _attendance_text(values.get("manager_decision"), "بانتظار الاعتماد")))
        c.drawRightString(width - 58, y - 70, _shape("الأمين العام: " + _attendance_text(values.get("secretary_decision"), "بانتظار الاعتماد")))
        c.drawRightString(width - 58, y - 92, _shape("الشؤون البشرية: " + _attendance_text(values.get("hr_decision"), "بانتظار الاعتماد")))
        if is_completed:
            c.setFillColor(colors.HexColor("#166534"))
            c.setFont(REGULAR_FONT, 9)
            c.drawString(48, y - 22, _shape("تمت تعبئة النموذج إلكترونياً"))

    return _attendance_form_canvas(data, title, body)
