from io import BytesIO
from datetime import datetime
from flask import send_file, abort
from flask_login import login_required, current_user
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph,
    Spacer, Table, TableStyle, PageBreak
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors

@workflow_bp.route("/<int:request_id>/pdf")
@login_required
def request_pdf(request_id):

    # ===== Load Request =====
    req = WorkflowRequest.query.get_or_404(request_id)

    # ===== Security =====
    if not can_access_request(req, current_user):
        abort(403)

    logs = (
        AuditLog.query
        .filter_by(request_id=request_id)
        .order_by(AuditLog.created_at.asc())
        .all()
    )

    attachments = [f for f in req.attachments if not f.is_deleted]

    buffer = BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=40,
        leftMargin=40,
        topMargin=50,
        bottomMargin=40
    )

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="Header",
        fontSize=14,
        leading=18,
        alignment=1,  # center
        spaceAfter=20
    ))

    styles.add(ParagraphStyle(
        name="Small",
        fontSize=9,
        textColor=colors.grey
    ))

    elements = []

    # ===== Header =====
    elements.append(
        Paragraph("Workflow Request Report", styles["Header"])
    )

    elements.append(
        Paragraph(f"<b>Request ID:</b> {req.id}", styles["Normal"])
    )
    elements.append(
        Paragraph(f"<b>Title:</b> {req.title}", styles["Normal"])
    )
    elements.append(
        Paragraph(f"<b>Status:</b> {req.status}", styles["Normal"])
    )
    elements.append(
        Paragraph(
            f"<b>Generated at:</b> "
            f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC",
            styles["Small"]
        )
    )

    Spacer(1, 20)
    elements.append(Spacer(1, 20))

    # ===== Attachments =====
    elements.append(
        Paragraph("<b>Attachments</b>", styles["Heading2"])
    )

    if attachments:
        att_table = [["#", "File Name", "Type", "Size (KB)", "Uploaded"]]
        for i, f in enumerate(attachments, start=1):
            att_table.append([
                i,
                f.original_name,
                f.mime_type or "-",
                round((f.file_size or 0) / 1024, 1),
                f.upload_date.strftime("%Y-%m-%d")
            ])

        elements.append(
            Table(
                att_table,
                colWidths=[30, 180, 80, 70, 80],
                style=TableStyle([
                    ("GRID", (0,0), (-1,-1), 0.5, colors.grey),
                    ("BACKGROUND", (0,0), (-1,0), colors.lightgrey),
                ])
            )
        )
    else:
        elements.append(
            Paragraph("No attachments.", styles["Normal"])
        )

    elements.append(PageBreak())

    # ===== Audit Log =====
    elements.append(
        Paragraph("<b>Workflow Timeline</b>", styles["Heading2"])
    )

    if logs:
        log_table = [["Date", "Action", "From → To", "Note"]]
        for log in logs:
            log_table.append([
                log.created_at.strftime("%Y-%m-%d %H:%M"),
                log.action,
                f"{log.old_status or '-'} → {log.new_status or '-'}",
                log.note or ""
            ])

        elements.append(
            Table(
                log_table,
                colWidths=[90, 90, 100, 160],
                style=TableStyle([
                    ("GRID", (0,0), (-1,-1), 0.5, colors.grey),
                    ("BACKGROUND", (0,0), (-1,0), colors.lightgrey),
                    ("VALIGN", (0,0), (-1,-1), "TOP")
                ])
            )
        )
    else:
        elements.append(
            Paragraph("No workflow actions recorded.", styles["Normal"])
        )

    # ===== Signed Stamp =====
    signed_attachments = [
        f for f in attachments if f.is_signed
    ]

    if signed_attachments:
        elements.append(Spacer(1, 20))
        elements.append(
            Paragraph(
                f"<b>Signed:</b> "
                f"Approved attachments signed by "
                f"{current_user.name} "
                f"on {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
                styles["Normal"]
            )
        )

    # ===== Build PDF =====
    doc.build(elements)

    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"workflow_request_{req.id}.pdf",
        mimetype="application/pdf"
    )
