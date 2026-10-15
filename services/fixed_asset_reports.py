"""Official fixed-asset committee report rendering helpers."""

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
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


_FONT_REGULAR = "FixedAssetReportArabic"
_FONT_BOLD = "FixedAssetReportArabicBold"
_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _register_fonts() -> tuple[str, str]:
    regular_path = _PROJECT_ROOT / "assets" / "fonts" / "DejaVuSans.ttf"
    bold_path = _PROJECT_ROOT / "assets" / "fonts" / "DejaVuSans-Bold.ttf"
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
    result = []
    for line in raw.splitlines() or [""]:
        shaped = line
        if _has_arabic(shaped):
            try:
                shaped = get_display(arabic_reshaper.reshape(shaped))
            except Exception:
                pass
        result.append(escape(shaped) or " ")
    return "<br/>".join(result)


def _p(value, style):
    return Paragraph(_shape_text(value), style)


def _label(value, mapping=None, default="-"):
    if value in (None, ""):
        return default
    return (mapping or {}).get(value, value)


def _current_snapshot(line):
    snapshot = dict(line.snapshot or {})
    if snapshot.get("included") is False:
        return None
    return {
        "tag": snapshot.get("tag") or (line.asset.asset_tag if line.asset else ""),
        "name": snapshot.get("name") or (line.asset.name if line.asset else ""),
        "serial": snapshot.get("serial") or (line.asset.serial_number if line.asset else ""),
        "location": snapshot.get("location") or "",
        "custodian": snapshot.get("custodian") or (line.document.employee.full_name if line.document and line.document.employee else ""),
        "condition": snapshot.get("condition") or "",
        "lifecycle": snapshot.get("target_lifecycle") or "",
        "result": snapshot.get("result") or "",
        "note": snapshot.get("inventory_note") or "",
    }


def _previous_snapshot(row):
    snapshot = dict(row.snapshot or {})
    return {
        "tag": snapshot.get("tag") or row.asset_tag,
        "name": snapshot.get("name") or "",
        "serial": snapshot.get("serial") or "",
        "location": snapshot.get("location") or "",
        "custodian": snapshot.get("custodian") or (row.employee.full_name if row.employee else ""),
        "condition": snapshot.get("condition") or "",
        "lifecycle": snapshot.get("lifecycle") or snapshot.get("status") or "",
        "result": snapshot.get("result") or "",
        "note": snapshot.get("note") or "",
    }


def _previous_rows_by_employee(cycle, manual_previous_by_employee):
    """Build the previous-year index once instead of scanning it per employee."""
    result = {}
    manual_previous_by_employee = manual_previous_by_employee or {}
    for employee_id, rows in manual_previous_by_employee.items():
        if rows:
            result[employee_id] = [_previous_snapshot(row) for row in rows]

    if not cycle.previous_cycle:
        return result
    for entry in cycle.previous_cycle.entries:
        if not entry.asset:
            continue
        custodian_id = (
            entry.observed_custodian_user_id
            if entry.scanned_at and entry.observed_custodian_user_id is not None
            else entry.expected_custodian_user_id
        )
        if custodian_id in result:
            continue
        result.setdefault(custodian_id, []).append({
            "tag": entry.asset.asset_tag,
            "name": entry.asset.name,
            "serial": entry.asset.serial_number or "",
            "location": entry.observed_location_label if entry.scanned_at else entry.expected_location_label,
            "custodian": entry.observed_custodian.full_name if entry.scanned_at and entry.observed_custodian else (entry.expected_custodian.full_name if entry.expected_custodian else ""),
            "condition": entry.observed_condition if entry.scanned_at else entry.expected_condition,
            "lifecycle": entry.observed_lifecycle_status if entry.scanned_at else entry.expected_lifecycle_status,
            "result": entry.result_status,
            "note": entry.note or "",
        })
    return result


def _employee_previous_rows(cycle, employee_id, manual_previous_lines):
    return _previous_rows_by_employee(
        cycle,
        {employee_id: manual_previous_lines} if manual_previous_lines else {},
    ).get(employee_id, [])


def _variance_rows(current_rows, previous_rows, condition_labels, lifecycle_labels, result_labels):
    current_by_tag = {str(row.get("tag") or "").casefold(): row for row in current_rows if row.get("tag")}
    previous_by_tag = {str(row.get("tag") or "").casefold(): row for row in previous_rows if row.get("tag")}
    keys = list(dict.fromkeys([*current_by_tag.keys(), *previous_by_tag.keys()]))
    rows = []
    for key in keys:
        current = current_by_tag.get(key)
        previous = previous_by_tag.get(key)
        differences = []
        for field, label, mapping in (
            ("name", "الاسم", None),
            ("serial", "الرقم التسلسلي", None),
            ("location", "الموقع", None),
            ("custodian", "صاحب العهدة", None),
            ("condition", "الحالة", condition_labels),
            ("lifecycle", "الوضع", lifecycle_labels),
            ("result", "نتيجة الجرد", result_labels),
            ("note", "الملاحظات", None),
        ):
            before = _label(previous.get(field) if previous else None, mapping, "غير موجود")
            after = _label(current.get(field) if current else None, mapping, "غير موجود")
            if before != after:
                differences.append(f"{label}: {before} ← {after}")
        if not differences:
            differences.append("لا توجد فروقات ظاهرة")
        rows.append({
            "tag": (current or previous or {}).get("tag") or "-",
            "previous": previous or {},
            "current": current or {},
            "differences": differences,
        })
    return rows


def build_committee_variance_index(
    cycle,
    documents,
    manual_previous_by_employee,
    *,
    condition_labels=None,
    lifecycle_labels=None,
    result_labels=None,
):
    """Return variance rows grouped by employee for the HTML preview."""
    condition_labels = condition_labels or {}
    lifecycle_labels = lifecycle_labels or {}
    result_labels = result_labels or {}
    grouped = {}
    for document in documents:
        grouped.setdefault(document.employee_id, []).append(document)

    previous_by_employee = _previous_rows_by_employee(cycle, manual_previous_by_employee)
    result = {}
    for employee_id, employee_documents in grouped.items():
        current_rows = []
        for document in employee_documents:
            current_rows.extend(
                row
                for line in document.lines
                if (row := _current_snapshot(line)) is not None
            )
        previous_rows = previous_by_employee.get(employee_id, [])
        result[employee_id] = _variance_rows(
            current_rows,
            previous_rows,
            condition_labels,
            lifecycle_labels,
            result_labels,
        )
    return result


def _styles(font_regular, font_bold):
    body = ParagraphStyle(
        "FixedAssetReportBody",
        fontName=font_regular,
        fontSize=9,
        leading=13,
        alignment=TA_RIGHT,
        wordWrap="RTL",
        spaceAfter=3,
    )
    return {
        "body": body,
        "center": ParagraphStyle("FixedAssetReportCenter", parent=body, alignment=TA_CENTER),
        "header": ParagraphStyle(
            "FixedAssetReportHeader",
            parent=body,
            fontName=font_bold,
            fontSize=9,
            leading=12,
            textColor=colors.white,
            alignment=TA_CENTER,
        ),
        "title": ParagraphStyle(
            "FixedAssetReportTitle",
            parent=body,
            fontName=font_bold,
            fontSize=18,
            leading=24,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#17365D"),
            spaceAfter=6,
        ),
        "section": ParagraphStyle(
            "FixedAssetReportSection",
            parent=body,
            fontName=font_bold,
            fontSize=12,
            leading=16,
            textColor=colors.HexColor("#17365D"),
            spaceBefore=5,
            spaceAfter=5,
        ),
        "employee": ParagraphStyle(
            "FixedAssetReportEmployee",
            parent=body,
            fontName=font_bold,
            fontSize=14,
            leading=19,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#17365D"),
            spaceAfter=5,
        ),
    }


def _table(data, widths, styles, *, repeat_rows=1):
    table = Table(data, colWidths=widths, repeatRows=repeat_rows, hAlign="RIGHT")
    commands = [
        ("GRID", (0, 0), (-1, -1), 0.45, colors.HexColor("#C9D2DC")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E78")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F6F8FA")]),
    ]
    table.setStyle(TableStyle(commands))
    return table


def _page_callback(header_path, font_regular):
    def draw(canvas, document):
        canvas.saveState()
        page_width, page_height = A4
        if header_path and Path(header_path).is_file():
            try:
                image = ImageReader(str(header_path))
                width, height = image.getSize()
                target_width = min(page_width - 28 * mm, 180 * mm)
                target_height = min(24 * mm, target_width * height / max(width, 1))
                canvas.drawImage(
                    image,
                    (page_width - target_width) / 2,
                    page_height - target_height - 8 * mm,
                    width=target_width,
                    height=target_height,
                    preserveAspectRatio=True,
                    mask="auto",
                )
            except Exception:
                pass
        canvas.setFont(font_regular, 7)
        canvas.setFillColor(colors.HexColor("#667085"))
        footer = f"صفحة {document.page}"
        try:
            footer = get_display(arabic_reshaper.reshape(footer))
        except Exception:
            pass
        canvas.drawCentredString(page_width / 2, 8 * mm, footer)
        canvas.restoreState()
    return draw


def build_committee_report_pdf(
    report,
    documents,
    manual_previous_by_employee,
    *,
    header_path=None,
    condition_labels=None,
    lifecycle_labels=None,
    result_labels=None,
):
    """Build a readable Arabic PDF with a page boundary before every employee."""
    condition_labels = condition_labels or {}
    lifecycle_labels = lifecycle_labels or {}
    result_labels = result_labels or {}
    font_regular, font_bold = _register_fonts()
    styles = _styles(font_regular, font_bold)
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=14 * mm,
        leftMargin=14 * mm,
        topMargin=35 * mm if header_path else 16 * mm,
        bottomMargin=14 * mm,
        title=f"تقرير جرد الأصول الثابتة {report.cycle.inventory_date}",
        author="Workflow PNCECS",
    )
    width = A4[0] - document.leftMargin - document.rightMargin
    story = []
    cycle = report.cycle
    story.append(_p("تقرير جرد الأصول الثابتة للموظفين", styles["title"]))
    story.append(_p(f"السنة / التاريخ: {cycle.inventory_date} · دورة الجرد: {cycle.code}", styles["center"]))
    committee_label = cycle.committee.label if cycle.committee else cycle.committee_name or "لجنة الجرد الثابت"
    story.append(_p(f"من: {committee_label} · إلى: الأمين العام", styles["center"]))
    if report.submitted_by:
        story.append(_p(f"رفع التقرير بواسطة: {report.submitted_by.full_name}", styles["center"]))
    story.append(Spacer(1, 4 * mm))
    if report.introduction:
        story.append(_p("مقدمة التقرير", styles["section"]))
        story.append(_p(report.introduction, styles["body"]))
    if report.points:
        story.append(_p("بنود التقرير", styles["section"]))
        story.append(_p(report.points, styles["body"]))
    if report.summary:
        story.append(_p("ملخص التقرير", styles["section"]))
        story.append(_p(report.summary, styles["body"]))

    grouped = []
    seen = set()
    for doc in documents:
        if doc.employee_id in seen:
            continue
        seen.add(doc.employee_id)
        grouped.append((doc.employee, [row for row in documents if row.employee_id == doc.employee_id]))
    previous_by_employee = _previous_rows_by_employee(cycle, manual_previous_by_employee)

    for employee_index, (employee, employee_documents) in enumerate(grouped):
        story.append(PageBreak())
        document_label = "، ".join(
            f"{doc.id}: قرار الموظف {_label(doc.employee_response, {'ACCEPT': 'وافق الموظف', 'REJECT': 'رفض الموظف', 'CHANGE': 'طلب مراجعة وتعديل'}, 'لم يقرر بعد')} · حالة المستند {_label(doc.status, {**{'PENDING': 'بانتظار الموظف', 'APPROVED': 'وافق الموظف', 'CHANGE_REQUESTED': 'طلب تعديل', 'REJECTED': 'مرفوض', 'REJECTED_ESCALATED': 'مرفوع للأمين العام', 'ISSUED': 'منفذ'},}, doc.status)}"
            for doc in employee_documents
        )
        story.append(_p(f"الموظف: {employee.full_name if employee else '-'}", styles["employee"]))
        story.append(_p(f"حالة العهدة: {document_label}", styles["center"]))
        story.append(Spacer(1, 2 * mm))

        current_rows = []
        for doc in employee_documents:
            current_rows.extend(row for line in doc.lines if (row := _current_snapshot(line)) is not None)
        custody_data = [[
            _p("م", styles["header"]),
            _p("الأصل / البيان", styles["header"]),
            _p("الرقم التسلسلي", styles["header"]),
            _p("الموقع", styles["header"]),
            _p("الحالة / الوضع", styles["header"]),
            _p("نتيجة الجرد", styles["header"]),
        ]]
        for number, row in enumerate(current_rows, start=1):
            custody_data.append([
                _p(number, styles["center"]),
                _p(f"{row['tag']}\n{row['name']}", styles["body"]),
                _p(row["serial"] or "-", styles["body"]),
                _p(row["location"] or "-", styles["body"]),
                _p(f"{_label(row['condition'], condition_labels)}\n{_label(row['lifecycle'], lifecycle_labels)}", styles["body"]),
                _p(_label(row["result"], result_labels), styles["body"]),
            ])
        if len(custody_data) == 1:
            custody_data.append([_p("-", styles["center"])] + [_p("لا توجد بنود ظاهرة", styles["body"])] * 5)
        story.append(_p("بنود العهدة حسب الجرد", styles["section"]))
        story.append(_table(custody_data, [10 * mm, 44 * mm, 29 * mm, 36 * mm, 35 * mm, 25 * mm], styles))
        story.append(Spacer(1, 4 * mm))

        previous_rows = previous_by_employee.get(employee.id if employee else None, [])
        variances = _variance_rows(current_rows, previous_rows, condition_labels, lifecycle_labels, result_labels)
        story.append(_p(f"تقرير الفروقات (السنة السابقة: {cycle.previous_cycle.inventory_date if cycle.previous_cycle else 'إدخال يدوي'})", styles["section"]))
        variance_data = [[
            _p("الأصل", styles["header"]),
            _p("السنة السابقة", styles["header"]),
            _p("السنة الحالية", styles["header"]),
            _p("الفرق والتفاصيل والإجراء", styles["header"]),
        ]]
        for row in variances:
            before = row["previous"]
            after = row["current"]
            variance_data.append([
                _p(row["tag"], styles["body"]),
                _p(f"{before.get('name') or '-'}\n{before.get('location') or '-'}\n{_label(before.get('condition'), condition_labels)}", styles["body"]),
                _p(f"{after.get('name') or '-'}\n{after.get('location') or '-'}\n{_label(after.get('condition'), condition_labels)}", styles["body"]),
                _p("\n".join(row["differences"]), styles["body"]),
            ])
        if len(variance_data) == 1:
            variance_data.append([_p("-", styles["center"]), _p("لا توجد بيانات سابقة", styles["body"]), _p("-", styles["body"]), _p("يُستكمل عند إدخال بيانات السنة السابقة.", styles["body"])])
        story.append(_table(variance_data, [25 * mm, 45 * mm, 45 * mm, 64 * mm], styles))

        employee_notes = [
            doc.employee_response_note
            for doc in employee_documents
            if doc.employee_response_note
        ]
        if employee_notes:
            story.append(Spacer(1, 3 * mm))
            story.append(_p("ملاحظات الموظف", styles["section"]))
            story.append(_p("\n".join(employee_notes), styles["body"]))

    document.build(
        story,
        onFirstPage=_page_callback(header_path, font_regular),
        onLaterPages=_page_callback(header_path, font_regular),
    )
    buffer.seek(0)
    return buffer
