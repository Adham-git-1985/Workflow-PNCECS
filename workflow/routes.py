import os
import uuid
import time
from io import BytesIO
from datetime import datetime

from flask import (
    send_file, abort, render_template,
    request, redirect, url_for,
    flash, jsonify, Response, current_app
)
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from reportlab.platypus import (
    SimpleDocTemplate, Paragraph,
    Spacer, Table, TableStyle, PageBreak
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors

from sqlalchemy import func

from . import workflow_bp
from extensions import db
from permissions import roles_required
from utils.permissions import can_access_request
from archive.routes import allowed_file, BASE_STORAGE

from models import (
    WorkflowRequest,
    ArchivedFile,
    AuditLog,
    Notification,
    User
)

from utils.events import emit_event


@workflow_bp.route("/<int:request_id>/pdf")
@login_required
def request_pdf(request_id):
    req = WorkflowRequest.query.get_or_404(request_id)

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
        alignment=1,
        spaceAfter=20
    ))

    styles.add(ParagraphStyle(
        name="Small",
        fontSize=9,
        textColor=colors.grey
    ))

    elements = []

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

    elements.append(Spacer(1, 20))

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
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ])
            )
        )
    else:
        elements.append(
            Paragraph("No attachments.", styles["Normal"])
        )

    elements.append(PageBreak())

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
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP")
                ])
            )
        )
    else:
        elements.append(
            Paragraph("No workflow actions recorded.", styles["Normal"])
        )

    signed_attachments = [f for f in attachments if f.is_signed]

    if signed_attachments:
        elements.append(Spacer(1, 20))
        elements.append(
            Paragraph(
                f"<b>Signed:</b> Approved attachments signed by "
                f"{current_user.email} on "
                f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
                styles["Normal"]
            )
        )

    doc.build(elements)
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"workflow_request_{req.id}.pdf",
        mimetype="application/pdf"
    )


@workflow_bp.route("/<int:request_id>/upload-attachment", methods=["POST"])
@login_required
def upload_attachment(request_id):
    req = WorkflowRequest.query.get_or_404(request_id)

    if not can_access_request(req, current_user):
        abort(403)

    file = request.files.get("file")
    description = request.form.get("description")

    if not file or file.filename == "":
        abort(400)

    original_name = secure_filename(file.filename)

    if not original_name or not allowed_file(original_name):
        abort(400)

    ext = original_name.rsplit(".", 1)[1].lower()
    stored_name = f"{uuid.uuid4().hex}.{ext}"
    file_path = os.path.join(BASE_STORAGE, stored_name)

    os.makedirs(BASE_STORAGE, exist_ok=True)
    file.save(file_path)

    attachment = ArchivedFile(
        original_name=original_name,
        stored_name=stored_name,
        description=description,
        file_path=file_path,
        mime_type=file.mimetype,
        file_size=os.path.getsize(file_path),
        owner_id=current_user.id,
        workflow_request_id=req.id,
        visibility="workflow"
    )

    db.session.add(attachment)

    emit_event(
        actor_id=current_user.id,
        action="WORKFLOW_ATTACHMENT_UPLOADED",
        message=f"Attachment uploaded to request #{req.id}",
        target_type="WorkflowRequest",
        target_id=req.id,
        notify_role="ADMIN"
    )

    db.session.commit()

    flash("Attachment uploaded successfully", "success")
    return redirect(url_for("workflow.view_request", request_id=req.id))


@workflow_bp.route("/attachment/<int:file_id>/download")
@login_required
def download_workflow_attachment(file_id):
    file = ArchivedFile.query.filter(
        ArchivedFile.id == file_id,
        ArchivedFile.workflow_request_id.isnot(None),
        ArchivedFile.is_deleted.is_(False)
    ).first_or_404()

    req = file.workflow_request

    if not can_access_request(req, current_user):
        abort(403)

    return send_file(
        file.file_path,
        as_attachment=True,
        download_name=file.original_name
    )


@workflow_bp.route("/notifications")
@login_required
def notifications():
    notes = (
        Notification.query
        .filter_by(user_id=current_user.id)
        .order_by(Notification.created_at.desc())
        .all()
    )
    return render_template(
        "notifications.html",
        notifications=notes
    )


@workflow_bp.route("/notifications/unread-count")
@login_required
def unread_notifications_count():
    count = Notification.query.filter_by(
        user_id=current_user.id,
        is_read=False
    ).count()

    return jsonify({"count": count})


@workflow_bp.route("/notifications/mark-all-read", methods=["POST"])
@login_required
def mark_all_notifications_read():
    Notification.query.filter_by(
        user_id=current_user.id,
        is_read=False
    ).update({"is_read": True})

    db.session.commit()
    flash("تم تعليم جميع الإشعارات كمقروءة", "success")
    return redirect(url_for("workflow.notifications"))


@workflow_bp.route("/notifications/stream")
@login_required
def notifications_stream():
    def event_stream(user_id):
        last_count = None

        while True:
            count = (
                Notification.query
                .filter_by(user_id=user_id, is_read=False)
                .count()
            )

            if count != last_count:
                yield f"data: {count}\n\n"
                last_count = count

            time.sleep(5)

    return Response(
        event_stream(current_user.id),
        mimetype="text/event-stream"
    )


@workflow_bp.route("/notifications/dashboard")
@login_required
@roles_required("ADMIN")
def notifications_dashboard():
    total = Notification.query.count()
    unread = Notification.query.filter_by(is_read=False).count()

    top_users = (
        db.session.query(
            User.email,
            func.count(Notification.id).label("count")
        )
        .join(Notification, Notification.user_id == User.id)
        .group_by(User.email)
        .order_by(func.count(Notification.id).desc())
        .limit(5)
        .all()
    )

    by_type = (
        db.session.query(
            Notification.type,
            func.count(Notification.id)
        )
        .group_by(Notification.type)
        .all()
    )

    return render_template(
        "notifications/dashboard.html",
        total=total,
        unread=unread,
        top_users=top_users,
        by_type=by_type
    )


@workflow_bp.route("/notifications/mark-read/<int:id>", methods=["POST"])
@login_required
def mark_notification_read(id):
    notif = Notification.query.get_or_404(id)

    if notif.user_id != current_user.id:
        abort(403)

    notif.is_read = True
    db.session.commit()
    return "", 204
