"""PDF export helpers for consolidated employee follow-up reports."""

from __future__ import annotations

from html import escape
from io import BytesIO
from pathlib import Path

import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


PDF_MIME = "application/pdf"
_FONT_REGULAR = "FollowupArabic"
_FONT_BOLD = "FollowupArabicBold"


def _font_path(filename: str) -> Path:
    return Path(__file__).resolve().parents[1] / "assets" / "fonts" / filename


def _register_fonts() -> tuple[str, str]:
    regular_path = _font_path("DejaVuSans.ttf")
    bold_path = _font_path("DejaVuSans-Bold.ttf")
    if _FONT_REGULAR not in pdfmetrics.getRegisteredFontNames() and regular_path.is_file():
        pdfmetrics.registerFont(TTFont(_FONT_REGULAR, str(regular_path)))
    if _FONT_BOLD not in pdfmetrics.getRegisteredFontNames() and bold_path.is_file():
        pdfmetrics.registerFont(TTFont(_FONT_BOLD, str(bold_path)))
    regular = _FONT_REGULAR if _FONT_REGULAR in pdfmetrics.getRegisteredFontNames() else "Helvetica"
    bold = _FONT_BOLD if _FONT_BOLD in pdfmetrics.getRegisteredFontNames() else regular
    return regular, bold


def _has_arabic(value: str) -> bool:
    return any("\u0600" <= character <= "\u06ff" for character in value)


def _shape_text(value) -> str:
    raw = "" if value is None else str(value)
    shaped_lines: list[str] = []
    for line in raw.splitlines() or [""]:
        candidate = line
        if _has_arabic(candidate):
            try:
                candidate = get_display(arabic_reshaper.reshape(candidate))
            except Exception:
                candidate = line
        shaped_lines.append(escape(candidate) or " ")
    return "<br/>".join(shaped_lines)


def _date_label(value) -> str:
    return value.strftime("%Y-%m-%d") if value else "-"


def _employee_name(report) -> str:
    employee = getattr(report, "employee", None)
    return (
        getattr(employee, "full_name", None)
        or getattr(employee, "name", None)
        or getattr(employee, "email", None)
        or "موظف غير محدد"
    ).strip()


def _manager_name(report) -> str:
    manager = getattr(report, "manager", None)
    return (
        getattr(manager, "full_name", None)
        or getattr(manager, "name", None)
        or getattr(manager, "email", None)
        or "غير محدد"
    ).strip()


def _organization_label(report, organization_contexts) -> str:
    context = (organization_contexts or {}).get(getattr(report, "id", None))
    if isinstance(context, dict):
        value = context.get("label") or context.get("path")
    else:
        value = context
    if isinstance(value, (list, tuple)):
        value = " / ".join(str(part).strip() for part in value if str(part).strip())
    return str(value or "غير محدد تنظيمياً").strip()


def _report_status_label(value: str | None) -> str:
    return {
        "DRAFT": "مسودة",
        "SUBMITTED": "مرسل للمدير",
        "NEEDS_REVISION": "يحتاج تعديلاً",
        "REVIEWED": "تمت المراجعة والاعتماد",
    }.get((value or "").upper(), value or "-")


def _rating_label(value: str | None) -> str:
    return {
        "EXCELLENT": "ممتاز",
        "GOOD": "جيد",
        "NEEDS_SUPPORT": "يحتاج دعماً",
    }.get((value or "").upper(), value or "-")


def _paragraph(text, style) -> Paragraph:
    return Paragraph(_shape_text(text), style)


def _table(data, *, widths, header_style, body_style, repeat_rows: int = 0) -> Table:
    table = Table(data, colWidths=widths, repeatRows=repeat_rows, hAlign="RIGHT")
    style_commands = [
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D9D9D9")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]
    if repeat_rows:
        style_commands.extend([
            ("BACKGROUND", (0, 0), (-1, repeat_rows - 1), colors.HexColor("#1F4E78")),
            ("TEXTCOLOR", (0, 0), (-1, repeat_rows - 1), colors.white),
        ])
    table.setStyle(TableStyle(style_commands))
    return table


def _styles(font_regular: str, font_bold: str) -> dict[str, ParagraphStyle]:
    body = ParagraphStyle(
        "FollowupPdfBody",
        fontName=font_regular,
        fontSize=11,
        leading=17,
        alignment=TA_RIGHT,
        wordWrap="RTL",
        spaceAfter=5,
    )
    return {
        "body": body,
        "body_center": ParagraphStyle(
            "FollowupPdfBodyCenter",
            parent=body,
            alignment=TA_CENTER,
        ),
        "label": ParagraphStyle(
            "FollowupPdfLabel",
            parent=body,
            fontName=font_bold,
            textColor=colors.HexColor("#1F4E78"),
        ),
        "header": ParagraphStyle(
            "FollowupPdfHeader",
            parent=body,
            fontName=font_bold,
            textColor=colors.white,
        ),
        "title": ParagraphStyle(
            "FollowupPdfTitle",
            parent=body,
            fontName=font_bold,
            fontSize=20,
            leading=28,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#1F4E78"),
            spaceAfter=8,
        ),
        "section": ParagraphStyle(
            "FollowupPdfSection",
            parent=body,
            fontName=font_bold,
            fontSize=16,
            leading=22,
            alignment=TA_CENTER,
            textColor=colors.white,
        ),
        "heading": ParagraphStyle(
            "FollowupPdfHeading",
            parent=body,
            fontName=font_bold,
            fontSize=12,
            leading=18,
            textColor=colors.HexColor("#1F4E78"),
            spaceBefore=8,
            spaceAfter=4,
        ),
    }


def _section_banner(text: str, width: float, style: ParagraphStyle) -> Table:
    banner = Table([[_paragraph(text, style)]], colWidths=[width], hAlign="RIGHT")
    banner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#1F4E78")),
        ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#153A5B")),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return banner


def _report_story(report, *, organization_label: str, number: int, doc_width: float, styles: dict):
    employee_name = _employee_name(report)
    story = [
        _section_banner(
            f"تقرير الموظف رقم {number}: {employee_name}",
            doc_width,
            styles["section"],
        ),
        Spacer(1, 5 * mm),
    ]
    details = [
        [_paragraph("البيان", styles["header"]), _paragraph("التفاصيل", styles["header"])],
        [_paragraph("الموظف", styles["label"]), _paragraph(employee_name, styles["body"])],
        [_paragraph("التبعية التنظيمية", styles["label"]), _paragraph(organization_label, styles["body"])],
        [_paragraph("المدير المباشر", styles["label"]), _paragraph(_manager_name(report), styles["body"])],
        [
            _paragraph("فترة التقرير", styles["label"]),
            _paragraph(
                f"{_date_label(getattr(report, 'period_start', None))} إلى "
                f"{_date_label(getattr(report, 'period_end', None))}",
                styles["body"],
            ),
        ],
        [
            _paragraph("الحالة", styles["label"]),
            _paragraph(_report_status_label(getattr(report, "status", None)), styles["body"]),
        ],
    ]
    story.append(
        _table(
            details,
            widths=[doc_width * 0.28, doc_width * 0.72],
            header_style=styles["header"],
            body_style=styles["body"],
            repeat_rows=1,
        )
    )
    story.append(Spacer(1, 4 * mm))

    items = [
        item
        for item in (getattr(report, "items", None) or [])
        if getattr(item, "is_included", True)
        and (getattr(item, "status", "") or "").upper() == "COMPLETED"
    ]
    item_rows = [[_paragraph("المهمة", styles["header"]), _paragraph("التاريخ", styles["header"])]]
    item_rows.extend([
        [
            _paragraph(getattr(item, "title", None) or "مهمة منجزة", styles["body"]),
            _paragraph(_date_label(getattr(item, "completed_on", None)), styles["body"]),
        ]
        for item in items
    ] or [[_paragraph("لا توجد مهام منجزة خلال فترة التقرير.", styles["body"]), _paragraph("-", styles["body"])]] )
    story.append(
        _table(
            item_rows,
            widths=[doc_width * 0.72, doc_width * 0.28],
            header_style=styles["header"],
            body_style=styles["body"],
            repeat_rows=1,
        )
    )

    employee_summary = (
        getattr(report, "employee_summary", None)
        or getattr(report, "ai_summary", None)
        or "-"
    )
    for label, value in (
        ("ملخص الإنجازات", employee_summary),
        ("التحديات أو الاحتياجات", getattr(report, "challenges", None) or "-"),
        ("المطلوب من المدير", getattr(report, "manager_request", None) or "-"),
    ):
        story.extend([
            _paragraph(label, styles["heading"]),
            _paragraph(value, styles["body"]),
        ])
    if getattr(report, "manager_comment", None) or getattr(report, "manager_rating", None):
        story.extend([
            _paragraph("مراجعة المدير", styles["heading"]),
            _paragraph(getattr(report, "manager_comment", None) or "-", styles["body"]),
            _paragraph(
                f"التقييم المختصر: {_rating_label(getattr(report, 'manager_rating', None))}",
                styles["body"],
            ),
        ])
    return story


def _draw_footer(canvas, document, font_name: str) -> None:
    canvas.saveState()
    canvas.setFont(font_name, 8)
    canvas.setFillColor(colors.HexColor("#666666"))
    canvas.drawCentredString(A4[0] / 2, 10 * mm, f"صفحة {document.page}")
    canvas.restoreState()


def build_followups_bundle_pdf(
    reports,
    *,
    organization_contexts: dict | None = None,
    title: str = "تقرير الإنجاز الموحّد",
) -> bytes:
    """Build a paginated Arabic PDF containing one separated section per employee."""
    reports = list(reports or [])
    if not reports:
        raise ValueError("at least one report is required")

    font_regular, font_bold = _register_fonts()
    styles = _styles(font_regular, font_bold)
    stream = BytesIO()
    document = SimpleDocTemplate(
        stream,
        pagesize=A4,
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=16 * mm,
        bottomMargin=18 * mm,
        title=title,
        author="Workflow-PNCECS",
    )
    story = [
        _paragraph(title, styles["title"]),
        _paragraph(
            f"عدد الموظفين: {len(reports)} - التقارير المعتمدة المرتبة حسب الهيكل التنظيمي",
            styles["body_center"],
        ),
    ]
    story.append(PageBreak())
    for index, report in enumerate(reports, start=1):
        if index > 1:
            story.append(PageBreak())
        story.extend(
            _report_story(
                report,
                organization_label=_organization_label(report, organization_contexts),
                number=index,
                doc_width=document.width,
                styles=styles,
            )
        )
    document.build(
        story,
        onFirstPage=lambda canvas, doc: _draw_footer(canvas, doc, font_regular),
        onLaterPages=lambda canvas, doc: _draw_footer(canvas, doc, font_regular),
    )
    return stream.getvalue()
