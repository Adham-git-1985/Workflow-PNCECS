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
from services.asset_custody import fixed_asset_report_details


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


def current_asset_report_snapshot(line):
    snapshot = dict(line.snapshot or {})
    if snapshot.get("included") is False:
        return None
    asset = line.asset
    details = fixed_asset_report_details(asset)
    return {
        "asset_id": asset.id if asset else line.asset_id,
        "tag": snapshot.get("tag") or (asset.asset_tag if asset else ""),
        "name": snapshot.get("name") or (asset.name if asset else ""),
        "serial": snapshot.get("serial") or (asset.serial_number if asset else "") or "",
        "location": snapshot.get("location") or (asset.location_label if asset else ""),
        "custodian": snapshot.get("custodian") or (line.document.employee.full_name if line.document and line.document.employee else ""),
        "condition": snapshot.get("condition") or "",
        "lifecycle": snapshot.get("target_lifecycle") or "",
        "result": snapshot.get("result") or "",
        "note": snapshot.get("inventory_note") or "",
        "item": snapshot.get("item") or details["item"],
        "category": snapshot.get("category") or details["category"],
        "warehouse": snapshot.get("warehouse") or details["warehouse"],
        "room": snapshot.get("room") or details["room"],
        "description": snapshot.get("description") or details["description"],
        "image_filename": snapshot.get("image_filename") or details["image_filename"],
    }


_current_snapshot = current_asset_report_snapshot


def _previous_snapshot(row):
    snapshot = dict(row.snapshot or {})
    asset = row.asset
    details = fixed_asset_report_details(asset)
    result = {
        "asset_id": asset.id if asset else row.asset_id,
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
    for key in ("item", "category", "warehouse", "room", "description", "image_filename"):
        if key in snapshot or asset is not None:
            result[key] = snapshot.get(key) or details[key]
    return result


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
            "asset_id": entry.asset.id,
            "tag": entry.asset.asset_tag,
            "name": entry.asset.name,
            "serial": entry.asset.serial_number or "",
            "location": entry.observed_location_label if entry.scanned_at else entry.expected_location_label,
            "custodian": entry.observed_custodian.full_name if entry.scanned_at and entry.observed_custodian else (entry.expected_custodian.full_name if entry.expected_custodian else ""),
            "condition": entry.observed_condition if entry.scanned_at else entry.expected_condition,
            "lifecycle": entry.observed_lifecycle_status if entry.scanned_at else entry.expected_lifecycle_status,
            "result": entry.result_status,
            "note": entry.note or "",
            **fixed_asset_report_details(entry.asset),
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
        changes = []
        for field, label, mapping in (
            ("name", "الاسم", None),
            ("serial", "الرقم التسلسلي", None),
            ("location", "الموقع", None),
            ("custodian", "صاحب العهدة", None),
            ("condition", "الحالة", condition_labels),
            ("lifecycle", "الوضع", lifecycle_labels),
            ("result", "نتيجة الجرد", result_labels),
            ("note", "الملاحظات", None),
            ("item", "صنف المستودع", None),
            ("category", "التصنيف", None),
            ("warehouse", "المستودع", None),
            ("room", "الغرفة", None),
            ("description", "الوصف", None),
        ):
            if field in {"item", "category", "warehouse", "room", "description"} and not previous:
                continue
            if field in {"item", "category", "warehouse", "room", "description"} and field not in previous:
                continue
            before = _label(previous.get(field) if previous else None, mapping, "غير موجود")
            after = _label(current.get(field) if current else None, mapping, "غير موجود")
            if before != after:
                changes.append({"field": label, "before": before, "after": after})
                differences.append(f"{label}: {before} ← {after}")
        if not differences:
            differences.append("لا توجد فروقات ظاهرة")
        rows.append({
            "tag": (current or previous or {}).get("tag") or "-",
            "previous": previous or {},
            "current": current or {},
            "differences": differences,
            "changes": changes,
            "has_differences": bool(changes),
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
        # An empty previous-year set is not a comparison.  Do not compare the
        # current snapshot with ``None``; that would incorrectly flag every
        # current asset as a difference when the cycle has no prior data.
        if not previous_rows:
            result[employee_id] = []
            continue
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


def _pdf_asset_cell(row, styles, image_resolver=None):
    details = "\n".join(
        (
            f"{row['tag']}",
            row["name"] or "-",
            f"صنف المستودع: {row.get('item') or '-'}",
            f"التصنيف: {row.get('category') or '-'}",
            f"المستودع: {row.get('warehouse') or '-'}",
            f"الغرفة: {row.get('room') or '-'}",
            f"الوصف: {row.get('description') or '-'}",
        )
    )
    path = None
    if image_resolver and row.get("image_filename"):
        try:
            candidate = image_resolver(row["image_filename"])
            if candidate and Path(candidate).is_file():
                path = candidate
        except (OSError, TypeError, ValueError):
            path = None
    if path is None:
        return _p(details, styles["body"])
    try:
        return [
            Image(str(path), width=18 * mm, height=18 * mm, kind="proportional"),
            Spacer(1, 1 * mm),
            _p(details, styles["body"]),
        ]
    except (OSError, ValueError):
        return _p(details, styles["body"])


def _pdf_variance_cell(row, styles):
    details = "\n".join(
        (
            f"الاسم: {row.get('name') or '-'}",
            f"الرقم التسلسلي: {row.get('serial') or '-'}",
            f"صنف المستودع: {row.get('item') or '-'}",
            f"التصنيف: {row.get('category') or '-'}",
            f"المستودع: {row.get('warehouse') or '-'}",
            f"الغرفة: {row.get('room') or '-'}",
            f"الموقع: {row.get('location') or '-'}",
            f"الحالة: {row.get('condition') or '-'}",
            f"الوصف: {row.get('description') or '-'}",
        )
    )
    return _p(details, styles["body"])


def build_committee_report_pdf(
    report,
    documents,
    manual_previous_by_employee,
    *,
    header_path=None,
    condition_labels=None,
    lifecycle_labels=None,
    result_labels=None,
    report_audits=None,
    image_resolver=None,
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
                _pdf_asset_cell(row, styles, image_resolver),
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
        variances = (
            _variance_rows(current_rows, previous_rows, condition_labels, lifecycle_labels, result_labels)
            if previous_rows
            else []
        )
        comparison_label = cycle.previous_cycle.inventory_date if cycle.previous_cycle else "لا توجد بيانات سابقة"
        story.append(_p(f"تقرير الفروقات (السنة السابقة: {comparison_label})", styles["section"]))
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
                _pdf_variance_cell({**before, "condition": _label(before.get("condition"), condition_labels)}, styles),
                _pdf_variance_cell({**after, "condition": _label(after.get("condition"), condition_labels)}, styles),
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

        audit_rows = [[
            _p("التاريخ", styles["header"]),
            _p("الإجراء", styles["header"]),
            _p("بواسطة", styles["header"]),
            _p("الملاحظة", styles["header"]),
        ]]
        for doc in employee_documents:
            for event in doc.events:
                audit_rows.append([
                    _p(event.created_at.strftime("%Y-%m-%d %H:%M") if event.created_at else "-", styles["body"]),
                    _p(event.action, styles["body"]),
                    _p(event.actor.full_name if event.actor else "-", styles["body"]),
                    _p(event.note or "-", styles["body"]),
                ])
        if len(audit_rows) > 1:
            story.append(Spacer(1, 3 * mm))
            story.append(_p("سجل إجراءات العهدة", styles["section"]))
            story.append(_table(audit_rows, [28 * mm, 34 * mm, 42 * mm, 78 * mm], styles))

    report_audits = report_audits or []
    report_audit_rows = [[
        _p("التاريخ", styles["header"]),
        _p("الإجراء", styles["header"]),
        _p("بواسطة", styles["header"]),
        _p("الملاحظة", styles["header"]),
    ]]
    for audit in report_audits:
        report_audit_rows.append([
            _p(audit.created_at.strftime("%Y-%m-%d %H:%M") if audit.created_at else "-", styles["body"]),
            _p(audit.action, styles["body"]),
            _p(audit.user.full_name if audit.user else "-", styles["body"]),
            _p(audit.note or "-", styles["body"]),
        ])
    if len(report_audit_rows) == 1:
        report_audit_rows.append([
            _p("-", styles["center"]),
            _p("لا يوجد سجل تقرير بعد", styles["body"]),
            _p("-", styles["body"]),
            _p("-", styles["body"]),
        ])
    story.append(PageBreak())
    story.append(_p("سجل التقرير", styles["section"]))
    story.append(_table(report_audit_rows, [28 * mm, 34 * mm, 42 * mm, 78 * mm], styles))

    document.build(
        story,
        onFirstPage=_page_callback(header_path, font_regular),
        onLaterPages=_page_callback(header_path, font_regular),
    )
    buffer.seek(0)
    return buffer
