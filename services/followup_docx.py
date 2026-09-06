"""Word export helpers for employee follow-up reports."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def is_valid_docx(path: str | Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            return "word/document.xml" in archive.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def _set_rtl(paragraph, *, bold: bool = False, size: int = 12) -> None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt

    paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    p_pr = paragraph._p.get_or_add_pPr()
    bidi = p_pr.find(qn("w:bidi"))
    if bidi is None:
        bidi = OxmlElement("w:bidi")
        p_pr.append(bidi)
    bidi.set(qn("w:val"), "1")
    for run in paragraph.runs:
        run.font.name = "Arial"
        run.font.size = Pt(size)
        run.bold = bold
        r_pr = run._element.get_or_add_rPr()
        fonts = r_pr.get_or_add_rFonts()
        fonts.set(qn("w:ascii"), "Arial")
        fonts.set(qn("w:hAnsi"), "Arial")
        fonts.set(qn("w:cs"), "Arial")


def _paragraph(document, value: str, *, bold: bool = False, size: int = 12):
    paragraph = document.add_paragraph(value or "-")
    _set_rtl(paragraph, bold=bold, size=size)
    return paragraph


def _date_label(value) -> str:
    return value.strftime("%Y-%m-%d") if value else "-"


def _status_label(value: str | None) -> str:
    return {
        "COMPLETED": "منجز",
        "INCOMPLETE": "غير مكتمل",
        "IN_PROGRESS": "قيد التنفيذ",
    }.get((value or "").upper(), value or "-")


def _set_cell(cell, value: str, *, header: bool = False) -> None:
    cell.text = str(value or "-")
    for paragraph in cell.paragraphs:
        _set_rtl(paragraph, bold=header, size=11)


def build_followup_docx(report, template_path: str | Path | None = None) -> bytes:
    """Build a reviewable report document and preserve an uploaded letterhead."""
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.shared import Pt

    path = Path(template_path) if template_path else None
    document = Document(str(path)) if path and path.is_file() else Document()
    try:
        normal = document.styles["Normal"]
        normal.font.name = "Arial"
        normal.font.size = Pt(12)
    except KeyError:
        pass

    title = document.add_paragraph("تقرير إنجاز الموظف")
    _set_rtl(title, bold=True, size=18)

    details = document.add_table(rows=0, cols=2)
    details.style = "Table Grid"
    details.alignment = WD_TABLE_ALIGNMENT.RIGHT
    for label, value in (
        ("الموظف", getattr(getattr(report, "employee", None), "full_name", "-")),
        ("المدير المباشر", getattr(getattr(report, "manager", None), "full_name", "-")),
        ("فترة التقرير", f"{_date_label(report.period_start)} إلى {_date_label(report.period_end)}"),
        ("الحالة", _status_label(report.status)),
    ):
        cells = details.add_row().cells
        _set_cell(cells[0], label, header=True)
        _set_cell(cells[1], value)

    _paragraph(document, "الإنجازات", bold=True, size=15)
    items = [item for item in (report.items or []) if getattr(item, "is_included", True)]
    if items:
        table = document.add_table(rows=1, cols=4)
        table.style = "Table Grid"
        table.alignment = WD_TABLE_ALIGNMENT.RIGHT
        for cell, label in zip(table.rows[0].cells, ("الإنجاز", "التفاصيل", "التاريخ", "الحالة")):
            _set_cell(cell, label, header=True)
        for item in items:
            cells = table.add_row().cells
            _set_cell(cells[0], item.title)
            _set_cell(cells[1], item.description or item.ai_suggestion or "-")
            _set_cell(cells[2], _date_label(item.completed_on))
            _set_cell(cells[3], _status_label(item.status))
    else:
        _paragraph(document, "لا توجد إنجازات مدرجة.")

    _paragraph(document, "ملخص الموظف", bold=True, size=15)
    _paragraph(document, report.employee_summary or report.ai_summary or "-")
    _paragraph(document, "التحديات أو الاحتياجات", bold=True, size=15)
    _paragraph(document, report.challenges or "-")
    _paragraph(document, "المطلوب من المدير", bold=True, size=15)
    _paragraph(document, report.manager_request or "-")

    if report.manager_comment or report.manager_rating:
        _paragraph(document, "مراجعة المدير", bold=True, size=15)
        _paragraph(document, report.manager_comment or "-")
        _paragraph(document, f"التقييم المختصر: {report.manager_rating or '-'}")

    stream = io.BytesIO()
    document.save(stream)
    return stream.getvalue()
