"""Report data and PDF rendering for attendance-delay workflows."""

from __future__ import annotations

import html
import os
from io import BytesIO
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from extensions import db
from models import (
    AuditLog,
    ArchivedFile,
    Department,
    Directorate,
    Division,
    HRAttendanceDelayRequest,
    HRLookupItem,
    OrgNode,
    RequestAttachment,
    Section,
    Unit,
    User,
    WorkflowInstanceStep,
    WorkflowStepTask,
)


REPORT_TITLE = "تقرير التأخير وتبرير التأخير"


def _user_name(user_or_id, user_map: dict[int, User] | None = None) -> str:
    if isinstance(user_or_id, User):
        user = user_or_id
    else:
        try:
            user = (user_map or {}).get(int(user_or_id)) if user_or_id else None
        except (TypeError, ValueError):
            user = None
    if not user:
        return "—"
    return (
        getattr(user, "full_name", None)
        or getattr(user, "name", None)
        or getattr(user, "email", None)
        or f"مستخدم #{getattr(user, 'id', '')}"
    ).strip()


def _date_time(value) -> str:
    if not value:
        return "—"
    try:
        return value.strftime("%Y-%m-%d %H:%M")
    except AttributeError:
        return str(value)


def _date_only(value) -> str:
    if not value:
        return "—"
    return str(value)


def _workflow_status_label(value: str | None) -> str:
    return {
        "DRAFT": "مسودة",
        "IN_PROGRESS": "قيد المعالجة",
        "APPROVED": "معتمد",
        "REJECTED": "مرفوض",
        "CLOSED": "مغلق",
    }.get((value or "").strip().upper(), value or "—")


def _step_status_label(value: str | None, response: str | None = None) -> str:
    normalized = (value or "PENDING").strip().upper()
    response_normalized = (response or "").strip().upper()
    if normalized == "RESPONDED":
        normalized = response_normalized or normalized
    return {
        "APPROVED": "تمت الموافقة",
        "REJECTED": "تم الرفض",
        "RESPONDED": "تم الرد",
        "BYPASSED": "تم التجاوز",
        "SKIPPED": "تم تجاوز المرحلة",
        "PENDING": "بانتظار الإجراء",
    }.get(normalized, value or "—")


def _stage_label(step: WorkflowInstanceStep | None) -> str:
    if not step:
        return "—"
    if (getattr(step, "routing_label", None) or "").strip():
        return step.routing_label.strip()
    return {
        1: "تعبئة تبرير الموظف",
        2: "اعتماد المدير المباشر",
        3: "اعتماد الموارد البشرية",
        4: "اعتماد الأمين العام",
        5: "الاعتماد النهائي للشؤون البشرية",
    }.get(int(getattr(step, "step_order", 0) or 0), "مرحلة من مسار التأخير")


def _step_target(step: WorkflowInstanceStep | None, user_map: dict[int, User]) -> str:
    if not step:
        return "—"
    kind = (getattr(step, "approver_kind", None) or "").strip().upper()
    if kind == "USER" and getattr(step, "approver_user_id", None):
        return _user_name(step.approver_user_id, user_map)
    if kind == "ROLE" and getattr(step, "approver_role", None):
        return str(step.approver_role)
    for attr in (
        "routing_node_label",
        "routing_job_title",
        "approver_role",
    ):
        value = (getattr(step, attr, None) or "").strip()
        if value:
            return value
    model_map = {
        "DEPARTMENT": (Department, "approver_department_id"),
        "DIRECTORATE": (Directorate, "approver_directorate_id"),
        "UNIT": (Unit, "approver_unit_id"),
        "SECTION": (Section, "approver_section_id"),
        "DIVISION": (Division, "approver_division_id"),
        "ORG_NODE": (OrgNode, "approver_org_node_id"),
    }
    model, attr = model_map.get(kind, (None, None))
    if model and attr:
        row = db.session.get(model, getattr(step, attr, None))
        if row:
            return (
                getattr(row, "name_ar", None)
                or getattr(row, "name", None)
                or getattr(row, "name_en", None)
                or f"{kind} #{row.id}"
            )
    return "—"


def _work_location(employee: User | None) -> str:
    employee_file = getattr(employee, "employee_file", None) if employee else None
    lookup_id = getattr(employee_file, "work_location_lookup_id", None)
    if not lookup_id:
        return "—"
    lookup = db.session.get(HRLookupItem, lookup_id)
    return (getattr(lookup, "label", None) or "—") if lookup else "—"


def _employee_number(employee: User | None) -> str:
    employee_file = getattr(employee, "employee_file", None) if employee else None
    return (getattr(employee_file, "employee_no", None) or "—").strip()


def _response_attachment_names(case: HRAttendanceDelayRequest) -> list[str]:
    names: list[str] = []
    payload = case.response_payload()
    document_name = (payload.get("document_name") or "").strip()
    if document_name:
        names.append(document_name)

    request_id = getattr(case, "workflow_request_id", None)
    if request_id:
        logs = (
            AuditLog.query
            .filter_by(
                request_id=request_id,
                action="WORKFLOW_ATTACHMENT_UPLOADED",
                target_type="ARCHIVE_FILE",
            )
            .filter(AuditLog.note.contains("source=ATTENDANCE_DELAY_EMPLOYEE_RESPONSE"))
            .order_by(AuditLog.id.asc())
            .all()
        )
        file_ids = [int(log.target_id) for log in logs if log.target_id]
        for archived_file_id in file_ids:
            archived = db.session.get(ArchivedFile, archived_file_id)
            if archived and getattr(archived, "original_name", None):
                names.append(archived.original_name)

        if not file_ids:
            attachments = (
                RequestAttachment.query
                .filter_by(request_id=request_id)
                .order_by(RequestAttachment.id.asc())
                .all()
            )
            for attachment in attachments:
                archived = getattr(attachment, "archived_file", None)
                name = getattr(archived, "original_name", None) if archived else None
                if name:
                    names.append(name)
    return list(dict.fromkeys(name for name in names if name))


def _collect_user_map(cases) -> dict[int, User]:
    ids: set[int] = set()
    for case in cases:
        request_row = getattr(case, "workflow_request", None)
        instance = getattr(request_row, "workflow_instance", None) if request_row else None
        for step in getattr(instance, "steps", None) or ():
            ids.update(
                int(value)
                for value in (
                    getattr(step, "approver_user_id", None),
                    getattr(step, "decided_by_id", None),
                )
                if value
            )
            tasks = (
                WorkflowStepTask.query
                .filter_by(instance_id=instance.id, step_order=step.step_order)
                .all()
                if instance
                else []
            )
            ids.update(int(task.assignee_user_id) for task in tasks if task.assignee_user_id)
            ids.update(int(task.bypassed_by_id) for task in tasks if task.bypassed_by_id)
        for value in (
            getattr(case, "employee_id", None),
            getattr(case, "initiated_by_id", None),
            getattr(case, "final_decision_by_id", None),
        ):
            if value:
                ids.add(int(value))
    return {
        int(user.id): user
        for user in (User.query.filter(User.id.in_(ids)).all() if ids else [])
    }


def _build_case_data(case: HRAttendanceDelayRequest, user_map: dict[int, User]) -> dict:
    request_row = getattr(case, "workflow_request", None)
    instance = getattr(request_row, "workflow_instance", None) if request_row else None
    steps = sorted(
        list(getattr(instance, "steps", None) or []),
        key=lambda row: int(getattr(row, "step_order", 0) or 0),
    )
    tasks_by_step: dict[int, list[WorkflowStepTask]] = {}
    if instance:
        tasks = (
            WorkflowStepTask.query
            .filter_by(instance_id=instance.id)
            .order_by(WorkflowStepTask.step_order.asc(), WorkflowStepTask.id.asc())
            .all()
        )
        for task in tasks:
            tasks_by_step.setdefault(int(task.step_order), []).append(task)

    workflow_status = (getattr(request_row, "status", None) or "IN_PROGRESS").upper()
    current_order = int(getattr(instance, "current_step_order", 0) or 0) if instance else 0
    current_step = next((step for step in steps if int(step.step_order) == current_order), None)
    if workflow_status == "APPROVED":
        reached_label = "اكتمل المسار واعتمد الطلب نهائياً"
        reached_target = "—"
    elif workflow_status in {"REJECTED", "CLOSED"}:
        rejected_step = next(
            (step for step in reversed(steps) if (step.status or "").upper() == "REJECTED"),
            None,
        )
        reached_label = "رُفض الطلب عند " + _stage_label(rejected_step or current_step)
        reached_target = _step_target(rejected_step or current_step, user_map)
    else:
        reached_label = _stage_label(current_step)
        current_tasks = tasks_by_step.get(current_order, [])
        pending_targets = [
            _user_name(task.assignee_user_id, user_map)
            for task in current_tasks
            if (task.status or "").upper() == "PENDING"
        ]
        reached_target = "، ".join(pending_targets) if pending_targets else _step_target(current_step, user_map)

    step_rows: list[dict] = []
    for step in steps:
        step_order = int(step.step_order or 0)
        label = _stage_label(step)
        step_tasks = tasks_by_step.get(step_order, [])
        if (step.mode or "").strip().upper() == "PARALLEL_SYNC" and step_tasks:
            for task in step_tasks:
                task_status = (task.status or "PENDING").upper()
                if task_status == "RESPONDED":
                    responder = _user_name(task.assignee_user_id, user_map)
                    action_at = task.responded_at
                    note = (task.note or "").replace("MENTION_TASK", "", 1).strip()
                elif task_status == "BYPASSED":
                    responder = (
                        "تم التجاوز بواسطة "
                        + _user_name(task.bypassed_by_id, user_map)
                        if task.bypassed_by_id
                        else "—"
                    )
                    action_at = task.bypassed_at
                    note = task.bypass_reason or "—"
                else:
                    responder = "—"
                    action_at = None
                    note = task.note or "—"
                step_rows.append({
                    "order": step_order,
                    "label": label,
                    "target": _user_name(task.assignee_user_id, user_map),
                    "status": _step_status_label(task_status, task.response),
                    "responder": responder,
                    "action_at": _date_time(action_at),
                    "note": note or "—",
                })
            continue

        decided_by = getattr(step, "decided_by_id", None)
        step_rows.append({
            "order": step_order,
            "label": label,
            "target": _step_target(step, user_map),
            "status": _step_status_label(getattr(step, "status", None)),
            "responder": _user_name(decided_by, user_map) if decided_by else "—",
            "action_at": _date_time(getattr(step, "decided_at", None)),
            "note": (getattr(step, "note", None) or "—").strip(),
        })

    employee = getattr(case, "employee", None)
    payload = case.response_payload()
    return {
        "request_id": getattr(request_row, "id", None) or getattr(case, "workflow_request_id", None),
        "request_title": getattr(request_row, "title", None) or "—",
        "request_description": getattr(request_row, "description", None) or "—",
        "request_status": _workflow_status_label(workflow_status),
        "created_at": _date_time(getattr(request_row, "created_at", None)),
        "employee_name": _user_name(employee),
        "employee_no": _employee_number(employee),
        "delay_day": _date_only(getattr(case, "delay_day", None)),
        "work_location": _work_location(employee),
        "first_in_time": getattr(case, "first_in_time", None) or "—",
        "late_minutes": int(getattr(case, "late_minutes", 0) or 0),
        "early_leave_minutes": int(getattr(case, "early_leave_minutes", 0) or 0),
        "initiated_by": _user_name(getattr(case, "initiated_by_id", None), user_map),
        "response_due_at": _date_time(getattr(case, "response_due_at", None)),
        "employee_response_status": (
            "تم تقديم التبرير" if getattr(case, "employee_responded_at", None)
            else "بانتظار تبرير الموظف"
        ),
        "employee_responded_at": _date_time(getattr(case, "employee_responded_at", None)),
        "case_kind": {"ABSENCE": "غياب", "DELAY": "تأخير"}.get(
            (payload.get("case_kind") or "DELAY").upper(),
            payload.get("case_kind") or "—",
        ),
        "reason": payload.get("reason") or "—",
        "employee_note": payload.get("note") or "—",
        "document_name": payload.get("document_name") or "—",
        "attachments": _response_attachment_names(case),
        "reached_label": reached_label,
        "reached_target": reached_target,
        "steps": step_rows,
    }


def attendance_delay_report_data(cases) -> list[dict]:
    """Return serializable report rows for the supplied delay cases."""
    cases = list(cases or [])
    user_map = _collect_user_map(cases)
    return [_build_case_data(case, user_map) for case in cases]


def _register_fonts() -> tuple[str, str]:
    regular_name = "AttendanceDelayReportArabic"
    bold_name = "AttendanceDelayReportArabicBold"
    candidates = (
        (regular_name, ("assets/fonts/DejaVuSans.ttf", r"C:\Windows\Fonts\arial.ttf")),
        (bold_name, ("assets/fonts/DejaVuSans-Bold.ttf", r"C:\Windows\Fonts\arialbd.ttf")),
    )
    from flask import current_app

    for name, paths in candidates:
        if name in pdfmetrics.getRegisteredFontNames():
            continue
        resolved = []
        for path in paths:
            resolved.append(path if path.startswith("C:\\") else current_app.root_path + "/" + path)
        selected = next((path for path in resolved if os.path.exists(path)), None)
        if selected:
            pdfmetrics.registerFont(TTFont(name, selected))
    registered = set(pdfmetrics.getRegisteredFontNames())
    return (
        regular_name if regular_name in registered else "Helvetica",
        bold_name if bold_name in registered else "Helvetica-Bold",
    )


def _pdf_text(value) -> str:
    import arabic_reshaper
    from bidi.algorithm import get_display

    text = "" if value is None else str(value)
    lines = []
    for line in text.splitlines() or [""]:
        shaped = line
        if any("\u0600" <= char <= "\u06ff" for char in shaped):
            try:
                shaped = get_display(arabic_reshaper.reshape(shaped))
            except Exception:
                pass
        lines.append(html.escape(shaped) or " ")
    return "<br/>".join(lines)


def _make_styles(font_regular: str, font_bold: str) -> dict[str, ParagraphStyle]:
    body = ParagraphStyle(
        "AttendanceDelayReportBody",
        fontName=font_regular,
        fontSize=8.5,
        leading=11.5,
        alignment=TA_RIGHT,
        wordWrap="RTL",
        spaceAfter=2,
    )
    return {
        "body": body,
        "small": ParagraphStyle("AttendanceDelayReportSmall", parent=body, fontSize=7, leading=9),
        "label": ParagraphStyle(
            "AttendanceDelayReportLabel",
            parent=body,
            fontName=font_bold,
            textColor=colors.HexColor("#17375E"),
        ),
        "header": ParagraphStyle(
            "AttendanceDelayReportHeader",
            parent=body,
            fontName=font_bold,
            fontSize=7.5,
            leading=9.5,
            alignment=TA_CENTER,
            textColor=colors.white,
        ),
        "title": ParagraphStyle(
            "AttendanceDelayReportTitle",
            parent=body,
            fontName=font_bold,
            fontSize=17,
            leading=21,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#17375E"),
            spaceAfter=3,
        ),
        "section": ParagraphStyle(
            "AttendanceDelayReportSection",
            parent=body,
            fontName=font_bold,
            fontSize=10.5,
            leading=13,
            alignment=TA_RIGHT,
            textColor=colors.white,
        ),
    }


def _p(value, style: ParagraphStyle) -> Paragraph:
    return Paragraph(_pdf_text(value), style)


def _section_banner(title: str, width: float, style: ParagraphStyle) -> Table:
    table = Table([[_p(title, style)]], colWidths=[width], hAlign="RIGHT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#17375E")),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#102A43")),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


def _details_table(data: dict, width: float, styles: dict[str, ParagraphStyle]) -> Table:
    rows = [
        ["رقم الطلب", data["request_id"], "حالة الطلب", data["request_status"]],
        ["الموظف", data["employee_name"], "الرقم الوظيفي", data["employee_no"]],
        ["يوم التأخير", data["delay_day"], "مقر العمل", data["work_location"]],
        ["أول دخول", data["first_in_time"], "التأخير / دقيقة", data["late_minutes"]],
        ["الخروج المبكر / دقيقة", data["early_leave_minutes"], "تاريخ إنشاء الطلب", data["created_at"]],
        ["المرحلة الحالية", data["reached_label"], "وصل إلى", data["reached_target"]],
        ["أنشأ النموذج", data["initiated_by"], "موعد رد الموظف", data["response_due_at"]],
        ["حالة تبرير الموظف", data["employee_response_status"], "تاريخ الرد", data["employee_responded_at"]],
    ]
    table_data = []
    for label_a, value_a, label_b, value_b in rows:
        table_data.append([
            _p(label_a, styles["label"]),
            _p(value_a, styles["body"]),
            _p(label_b, styles["label"]),
            _p(value_b, styles["body"]),
        ])
    label_width = width * 0.12
    value_width = width * 0.38
    table = Table(table_data, colWidths=[label_width, value_width, label_width, value_width], hAlign="RIGHT")
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#C9D3DF")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F2F6FA")),
        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#F2F6FA")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return table


def _justification_table(data: dict, width: float, styles: dict[str, ParagraphStyle]) -> Table:
    attachments = "، ".join(data.get("attachments") or []) or data.get("document_name") or "لا يوجد"
    rows = [
        ["نوع الحالة", data.get("case_kind"), "المرفقات المؤيدة", attachments],
        ["سبب التبرير", data.get("reason"), "ملاحظة الموظف", data.get("employee_note")],
        ["تفاصيل الطلب الأصلية", data.get("request_description"), "عنوان الطلب", data.get("request_title")],
    ]
    table_data = [[
        _p(label_a, styles["label"]),
        _p(value_a, styles["body"]),
        _p(label_b, styles["label"]),
        _p(value_b, styles["body"]),
    ] for label_a, value_a, label_b, value_b in rows]
    label_width = width * 0.12
    value_width = width * 0.38
    table = Table(table_data, colWidths=[label_width, value_width, label_width, value_width], hAlign="RIGHT")
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#C9D3DF")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F2F6FA")),
        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#F2F6FA")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def _steps_table(data: dict, width: float, styles: dict[str, ParagraphStyle]) -> Table:
    headers = ["الخطوة", "المرحلة", "الجهة / الشخص", "الحالة", "من جاوب", "وقت الإجراء", "الملاحظة / الرد"]
    rows = [[_p(value, styles["header"]) for value in headers]]
    for row in data.get("steps") or []:
        rows.append([
            _p(row.get("order"), styles["small"]),
            _p(row.get("label"), styles["small"]),
            _p(row.get("target"), styles["small"]),
            _p(row.get("status"), styles["small"]),
            _p(row.get("responder"), styles["small"]),
            _p(row.get("action_at"), styles["small"]),
            _p(row.get("note"), styles["small"]),
        ])
    if len(rows) == 1:
        rows.append([_p("لا توجد خطوات منشأة", styles["small"])] + [_p("—", styles["small"]) for _ in headers[1:]])
    proportions = (0.045, 0.14, 0.18, 0.09, 0.16, 0.12, 0.265)
    table = Table(rows, colWidths=[width * value for value in proportions], repeatRows=1, hAlign="RIGHT")
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#C9D3DF")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#17375E")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
    ]))
    return table


def _draw_footer(canvas, document, font_regular: str) -> None:
    canvas.saveState()
    canvas.setFont(font_regular, 7)
    canvas.setFillColor(colors.HexColor("#666666"))
    canvas.drawCentredString(landscape(A4)[0] / 2, 8 * mm, f"صفحة {document.page}")
    canvas.restoreState()


def build_attendance_delay_tracking_pdf(cases, *, title: str = REPORT_TITLE) -> bytes:
    """Build an A4 landscape PDF with one complete delay case per page."""
    rows = attendance_delay_report_data(cases)
    font_regular, font_bold = _register_fonts()
    styles = _make_styles(font_regular, font_bold)
    stream = BytesIO()
    document = SimpleDocTemplate(
        stream,
        pagesize=landscape(A4),
        rightMargin=12 * mm,
        leftMargin=12 * mm,
        topMargin=10 * mm,
        bottomMargin=13 * mm,
        title=title,
        author="Workflow-PNCECS",
    )
    width = document.width
    story = [
        _p(title, styles["title"]),
        _p(f"عدد الطلبات: {len(rows)} · تاريخ الإصدار: {_date_time(datetime.utcnow())}", styles["body"]),
        Spacer(1, 4 * mm),
    ]
    if not rows:
        story.append(_p("لا توجد طلبات تأخير مطابقة للمعايير المحددة.", styles["body"]))
    else:
        for index, data in enumerate(rows):
            if index:
                story.append(PageBreak())
            story.extend([
                _section_banner(f"بيانات الطلب #{data['request_id']}", width, styles["section"]),
                Spacer(1, 2 * mm),
                _details_table(data, width, styles),
                Spacer(1, 2 * mm),
                _section_banner("التأخير والتبرير", width, styles["section"]),
                Spacer(1, 2 * mm),
                _justification_table(data, width, styles),
                Spacer(1, 2 * mm),
                _section_banner("تتبع مسار الطلب والردود", width, styles["section"]),
                Spacer(1, 2 * mm),
                _steps_table(data, width, styles),
            ])
    document.build(
        story,
        onFirstPage=lambda canvas, doc: _draw_footer(canvas, doc, font_regular),
        onLaterPages=lambda canvas, doc: _draw_footer(canvas, doc, font_regular),
    )
    return stream.getvalue()
