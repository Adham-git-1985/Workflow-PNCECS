from __future__ import annotations

import os
from datetime import datetime
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from utils.corr_stamps import _shape_arabic


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FONT_DIR = PROJECT_ROOT / "assets" / "fonts"
WINDOWS_FONT_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
REGULAR_FONT = "TransportFormsSakkalMajalla"
BOLD_FONT = "TransportFormsSakkalMajallaBold"
FORM_FONT_SIZE = 16


def _shape_transport_text(value: str) -> str:
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        reshaper = arabic_reshaper.ArabicReshaper({"support_ligatures": False})
        return get_display(reshaper.reshape(value or ""))
    except Exception:
        return _shape_arabic(value or "")


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


def _pdf_text(value) -> str:
    raw = str(value if value not in (None, "") else "-").strip()
    return escape(_shape_transport_text(raw)).replace("\n", "<br/>")


def _plain(value, default: str = "-") -> str:
    value = str(value or "").strip()
    return value or default


def _styles() -> dict[str, ParagraphStyle]:
    regular, bold = _register_fonts()
    sample = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "TransportFormTitle",
            parent=sample["Title"],
            fontName=bold,
            fontSize=FORM_FONT_SIZE,
            leading=20,
            alignment=TA_CENTER,
            textColor=colors.black,
            spaceAfter=6,
            wordWrap="RTL",
        ),
        "subtitle": ParagraphStyle(
            "TransportFormSubtitle",
            parent=sample["Heading2"],
            fontName=bold,
            fontSize=FORM_FONT_SIZE,
            leading=20,
            alignment=TA_RIGHT,
            textColor=colors.black,
            spaceAfter=4,
            wordWrap="RTL",
        ),
        "body": ParagraphStyle(
            "TransportFormBody",
            parent=sample["BodyText"],
            fontName=regular,
            fontSize=FORM_FONT_SIZE,
            leading=20,
            alignment=TA_RIGHT,
            textColor=colors.black,
            spaceAfter=3,
            wordWrap="RTL",
        ),
        "body_bold": ParagraphStyle(
            "TransportFormBodyBold",
            parent=sample["BodyText"],
            fontName=bold,
            fontSize=FORM_FONT_SIZE,
            leading=20,
            alignment=TA_RIGHT,
            textColor=colors.black,
            spaceAfter=3,
            wordWrap="RTL",
        ),
        "table": ParagraphStyle(
            "TransportFormTable",
            parent=sample["BodyText"],
            fontName=regular,
            fontSize=FORM_FONT_SIZE,
            leading=18,
            alignment=TA_CENTER,
            textColor=colors.black,
            wordWrap="RTL",
        ),
        "table_bold": ParagraphStyle(
            "TransportFormTableBold",
            parent=sample["BodyText"],
            fontName=bold,
            fontSize=FORM_FONT_SIZE,
            leading=18,
            alignment=TA_CENTER,
            textColor=colors.black,
            wordWrap="RTL",
        ),
        "field_label": ParagraphStyle(
            "TransportFormFieldLabel",
            parent=sample["BodyText"],
            fontName=bold,
            fontSize=FORM_FONT_SIZE,
            leading=19,
            alignment=TA_RIGHT,
            wordWrap="RTL",
        ),
        "field_value": ParagraphStyle(
            "TransportFormFieldValue",
            parent=sample["BodyText"],
            fontName=regular,
            fontSize=FORM_FONT_SIZE,
            leading=19,
            alignment=TA_RIGHT,
            wordWrap="RTL",
        ),
    }


def _p(value, style: ParagraphStyle) -> Paragraph:
    return Paragraph(_pdf_text(value), style)


def _header_callback(letterhead_path: str | None, styles: dict[str, ParagraphStyle]):
    path = Path(letterhead_path) if letterhead_path else None
    image_path = path if path and path.suffix.lower() in {".png", ".jpg", ".jpeg"} else None
    image_payload = image_path.read_bytes() if image_path and image_path.exists() else None

    def draw_header(canvas, _doc):
        canvas.saveState()
        if image_payload:
            image = ImageReader(BytesIO(image_payload))
            width, height = image.getSize()
            max_width = A4[0] - (20 * mm)
            max_height = 34 * mm
            scale = min(max_width / width, max_height / height)
            draw_width = width * scale
            draw_height = height * scale
            canvas.drawImage(
                image,
                (A4[0] - draw_width) / 2,
                A4[1] - draw_height - (6 * mm),
                width=draw_width,
                height=draw_height,
                preserveAspectRatio=True,
                mask="auto",
            )
        elif not (path and path.suffix.lower() == ".pdf"):
            header = _p(
                "دولة فلسطين - اللجنة الوطنية الفلسطينية للتربية والثقافة والعلوم",
                styles["body_bold"],
            )
            header.wrapOn(canvas, A4[0] - 30 * mm, 22 * mm)
            header.drawOn(canvas, 15 * mm, A4[1] - 22 * mm)
            canvas.setStrokeColor(colors.HexColor("#555555"))
            canvas.line(15 * mm, A4[1] - 27 * mm, A4[0] - 15 * mm, A4[1] - 27 * mm)
        canvas.restoreState()

    return draw_header


def _merge_pdf_letterhead(pdf_bytes: bytes, letterhead_path: str | None) -> bytes:
    path = Path(letterhead_path) if letterhead_path else None
    if not path or path.suffix.lower() != ".pdf" or not path.exists():
        return pdf_bytes

    import fitz

    background = fitz.open(str(path))
    overlay = fitz.open(stream=pdf_bytes, filetype="pdf")
    result = fitz.open()
    try:
        for index in range(overlay.page_count):
            overlay_page = overlay[index]
            page = result.new_page(width=overlay_page.rect.width, height=overlay_page.rect.height)
            background_index = min(index, background.page_count - 1)
            page.show_pdf_page(page.rect, background, background_index, keep_proportion=False)
            page.show_pdf_page(page.rect, overlay, index, overlay=True, keep_proportion=False)
        return result.tobytes(garbage=4, deflate=True)
    finally:
        result.close()
        overlay.close()
        background.close()


def _build_document(story: list, letterhead_path: str | None) -> bytes:
    styles = _styles()
    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=42 * mm,
        bottomMargin=20 * mm,
        title="نماذج الحركة والنقل",
        author="نظام مسار",
    )
    callback = _header_callback(letterhead_path, styles)
    document.build(story, onFirstPage=callback, onLaterPages=callback)
    return _merge_pdf_letterhead(output.getvalue(), letterhead_path)


def _grid_table(data, widths, *, header_rows: int = 1, row_heights=None) -> Table:
    regular, bold = _register_fonts()
    table = Table(data, colWidths=widths, rowHeights=row_heights, hAlign="RIGHT", repeatRows=header_rows)
    commands = [
        ("GRID", (0, 0), (-1, -1), 0.75, colors.HexColor("#333333")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONTNAME", (0, 0), (-1, -1), regular),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    if header_rows:
        commands.extend([
            ("BACKGROUND", (0, 0), (-1, header_rows - 1), colors.HexColor("#F1F3F5")),
            ("FONTNAME", (0, 0), (-1, header_rows - 1), bold),
        ])
    table.setStyle(TableStyle(commands))
    return table


def _date_text(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y")
    raw = str(value or "").strip()
    if not raw:
        return datetime.now().strftime("%d/%m/%Y")
    try:
        return datetime.fromisoformat(raw).strftime("%d/%m/%Y")
    except ValueError:
        return raw


def _datetime_text(value) -> str:
    if not value:
        return "-"
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y %H:%M")
    return _plain(value)


def _time_text(value) -> str:
    if not value:
        return "-"
    if isinstance(value, datetime):
        return value.strftime("%H:%M")
    return _plain(value)


def build_vehicle_license_pdf(vehicle, letterhead_path: str | None = None) -> bytes:
    styles = _styles()
    vehicle_type = " ".join(filter(None, [_plain(getattr(vehicle, "vehicle_type", None), ""), _plain(getattr(vehicle, "model", None), "")])).strip() or "-"
    assigned_to = _plain(getattr(vehicle, "assigned_to", None), _plain(getattr(vehicle, "label", None)))
    date_value = datetime.now().strftime("%d/%m/%Y")
    reference = f"ح/ترخيص/{getattr(vehicle, 'id', '')}"

    story = [
        _p("كتاب ترخيص مركبة حكومية", styles["title"]),
        _p(f"الرقم: {reference}", styles["body"]),
        _p(f"التاريخ: {date_value}", styles["body"]),
        Spacer(1, 5 * mm),
        _p("الأخ المحترم / مدير عام النقل الحكومي", styles["subtitle"]),
        _p("وزارة النقل والمواصلات", styles["body_bold"]),
        Spacer(1, 3 * mm),
        _p("الموضوع: ترخيص مركبة حكومية خاصة باللجنة الوطنية الفلسطينية للتربية والثقافة والعلوم", styles["body_bold"]),
        _p(
            "تهديكم اللجنة الوطنية الفلسطينية للتربية والثقافة والعلوم أطيب تحياتها، وبالإشارة إلى الموضوع أعلاه، نرجو التكرم بإصدار رخصة المركبة الحكومية المذكورة بياناتها أدناه.",
            styles["body"],
        ),
        Spacer(1, 3 * mm),
    ]
    header = [
        _p("المستخدم", styles["table_bold"]),
        _p("سنة الإنتاج", styles["table_bold"]),
        _p("النوع", styles["table_bold"]),
        _p("الرقم الحكومي", styles["table_bold"]),
    ]
    values = [
        _p(assigned_to, styles["table"]),
        _p(getattr(vehicle, "year", None), styles["table"]),
        _p(vehicle_type, styles["table"]),
        _p(getattr(vehicle, "plate_no", None), styles["table"]),
    ]
    story.extend([
        _grid_table([header, values], [42 * mm, 31 * mm, 47 * mm, 35 * mm], row_heights=[10 * mm, 13 * mm]),
        Spacer(1, 5 * mm),
        _p("علمًا بأنه لا مانع لدينا من خصم رسوم الترخيص السنوية من حساب اللجنة لدى وزارة المالية والتخطيط.", styles["body"]),
        Spacer(1, 7 * mm),
        _p("وتفضلوا بقبول فائق الاحترام والتقدير،", styles["body_bold"]),
        Spacer(1, 12 * mm),
        _p("الأمين العام", styles["body_bold"]),
    ])
    return _build_document(story, letterhead_path)


def build_maintenance_request_pdf(maintenance, items, letterhead_path: str | None = None) -> bytes:
    styles = _styles()
    vehicle = maintenance.vehicle
    date_value = _date_text(getattr(maintenance, "invoice_day", None) or getattr(maintenance, "created_at", None))
    vehicle_type = " ".join(filter(None, [_plain(getattr(vehicle, "vehicle_type", None), ""), _plain(getattr(vehicle, "model", None), "")])).strip() or "-"

    story = [
        _p("طلب صيانة مركبة", styles["title"]),
        _p(f"التاريخ: {date_value}", styles["body"]),
        Spacer(1, 3 * mm),
        _p("الأخ / أمين عام اللجنة الوطنية الفلسطينية للتربية والثقافة والعلوم", styles["subtitle"]),
        _p("الموضوع: طلب صيانة مركبة", styles["body_bold"]),
        _p("يرجى التكرم بالموافقة على إجراء أعمال الصيانة التالية:", styles["body"]),
        Spacer(1, 2 * mm),
    ]
    vehicle_header = [
        _p("سنة الإنتاج", styles["table_bold"]),
        _p("رقم العداد", styles["table_bold"]),
        _p("رقم المحرك", styles["table_bold"]),
        _p("رقم الشاصي", styles["table_bold"]),
        _p("رقم المركبة", styles["table_bold"]),
        _p("نوع المركبة", styles["table_bold"]),
    ]
    vehicle_values = [
        _p(getattr(vehicle, "year", None), styles["table"]),
        _p(getattr(vehicle, "odometer_no", None) or getattr(vehicle, "current_odometer", None), styles["table"]),
        _p(getattr(vehicle, "engine_no", None), styles["table"]),
        _p(getattr(vehicle, "chassis_no", None), styles["table"]),
        _p(getattr(vehicle, "plate_no", None), styles["table"]),
        _p(vehicle_type, styles["table"]),
    ]
    story.extend([
        _grid_table([vehicle_header, vehicle_values], [24 * mm, 25 * mm, 27 * mm, 30 * mm, 25 * mm, 29 * mm], row_heights=[11 * mm, 12 * mm]),
        Spacer(1, 4 * mm),
    ])

    requested = []
    for item in list(items or [])[:8]:
        label = getattr(getattr(item, "maintenance_type_lookup", None), "label", None)
        note = getattr(item, "note", None)
        description = " - ".join(part for part in (label, note) if part) or "بند صيانة"
        requested.append(description if len(description) <= 180 else description[:177].rstrip() + "...")
    if not requested and getattr(maintenance, "notes", None):
        requested = [line.strip() for line in str(maintenance.notes).splitlines() if line.strip()][:8]
    requested.extend([""] * (8 - len(requested)))

    requested_rows = [[
        _p("البيانات المطلوبة لأعمال الصيانة", styles["table_bold"]),
        _p("الرقم", styles["table_bold"]),
    ]]
    requested_heights = [9 * mm]
    for index, description in enumerate(requested, start=1):
        requested_rows.append([
            _p(description if description else " ", styles["table"]),
            _p(index, styles["table"]),
        ])
        requested_heights.append(None if description else 7 * mm)
    story.extend([
        _grid_table(requested_rows, [143 * mm, 17 * mm], row_heights=requested_heights),
        Spacer(1, 8 * mm),
        KeepTogether([
            Table(
                [[_p("توقيع أمين عام اللجنة الوطنية", styles["body_bold"]), _p("توقيع مسؤول الحركة", styles["body_bold"])]],
                colWidths=[80 * mm, 80 * mm],
                hAlign="RIGHT",
                style=TableStyle([
                    ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]),
            ),
        ]),
    ])
    return _build_document(story, letterhead_path)


def build_movement_permit_pdf(permit, trip=None, letterhead_path: str | None = None) -> bytes:
    styles = _styles()
    vehicle = getattr(permit, "vehicle", None)
    driver = getattr(permit, "driver", None)
    requester = getattr(permit, "requester", None)
    origin = getattr(getattr(permit, "origin_zone", None), "name", None) or getattr(permit, "origin_text", None)
    destination = getattr(getattr(permit, "dest_zone", None), "name", None) or getattr(permit, "dest_text", None)
    route_text = f"من: {_plain(origin)}\nإلى: {_plain(destination)}"
    if getattr(permit, "purpose", None):
        route_text += f"\nالغرض: {_plain(permit.purpose)}"
    depart_at = getattr(trip, "started_at", None) or getattr(permit, "depart_at", None)
    return_at = getattr(trip, "ended_at", None) or getattr(permit, "return_at", None)
    order_no = getattr(trip, "order_no", None) or getattr(permit, "ref_no", None) or getattr(permit, "id", None)
    requester_name = getattr(requester, "full_name", None) or getattr(requester, "name", None) or getattr(requester, "email", None)

    story = [
        _p("نموذج رقم (1)", styles["title"]),
        _p(f"تصريح أمر حركة رقم ({_plain(order_no)})", styles["title"]),
        _p(f"اليوم والتاريخ: {_date_text(depart_at)}", styles["body"]),
        Spacer(1, 2 * mm),
    ]
    fields = [
        ("اسم السائق", getattr(driver, "name", None)),
        ("رقم السيارة", getattr(vehicle, "plate_no", None)),
        ("اسم الموظف المكلف بالمهمة", requester_name),
        ("خط سير الرحلة مع العنوان المستهدف", route_text),
        ("ساعة بدء المهمة", _time_text(depart_at)),
        ("ساعة نهاية المهمة", _time_text(return_at)),
        ("رقم العداد في بداية المهمة", getattr(trip, "start_odometer", None) if trip else getattr(vehicle, "current_odometer", None)),
        ("رقم العداد في نهاية المهمة", getattr(trip, "end_odometer", None) if trip else None),
    ]
    field_rows = []
    for label, value in fields:
        field_rows.append([
            _p(value, styles["field_value"]),
            _p(label, styles["field_label"]),
        ])
    field_table = Table(field_rows, colWidths=[105 * mm, 55 * mm], hAlign="RIGHT")
    field_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.65, colors.HexColor("#555555")),
        ("BACKGROUND", (1, 0), (1, -1), colors.HexColor("#F5F5F5")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.extend([
        field_table,
        Spacer(1, 11 * mm),
        _p("توقيع المكلف بالمهمة: ______________________________", styles["body_bold"]),
        Spacer(1, 7 * mm),
        _p("توقيع السائق: ______________________________________", styles["body_bold"]),
        Spacer(1, 10 * mm),
        _p("اعتماد مسؤول الحركة: _______________________________", styles["body_bold"]),
    ])
    return _build_document(story, letterhead_path)
