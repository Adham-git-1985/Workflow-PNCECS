# workflow/routes.py

import os
import uuid
import json
import time
import logging
from io import BytesIO
from datetime import datetime, timedelta

from flask import (
    send_file, abort, render_template,
    request, redirect, url_for,
    flash, jsonify, Response, stream_with_context
)
from flask_login import login_required, current_user

from reportlab.platypus import (
    SimpleDocTemplate, Paragraph,
    Spacer, Table, TableStyle, PageBreak
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors

from sqlalchemy import func, update, or_
from sqlalchemy.orm import joinedload

from . import workflow_bp
from extensions import db
from permissions import roles_required
from utils.permissions import can_access_request
from utils.events import emit_event

from models import (
    WorkflowRequest,
    ArchivedFile,
    AuditLog,
    Notification,
    User,
    WorkflowTemplate,
    WorkflowInstance,
    WorkflowInstanceStep,
    RequestEscalation,
    RequestAttachment,
    Approval,
    RequestType,
    WorkflowRoutingRule,
    Department,
    Directorate,
    Organization,

)

from workflow.engine import start_workflow_for_request, decide_step

logger = logging.getLogger(__name__)

# =========================
# Storage (same as archive)
# =========================
BASE_STORAGE = os.path.join(os.getcwd(), "storage", "archive")

ALLOWED_EXTENSIONS = {
    # Documents
    "pdf", "txt", "rtf",
    "doc", "docx", "odt",
    "xls", "xlsx", "ods", "csv",
    "ppt", "pptx", "odp",

    # Images
    "png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff",

    # Archives (common in institutions)
    "zip", "rar", "7z",

    # Audio/Video (optional but common)
    "mp3", "wav", "m4a", "mp4", "mov", "avi",
<<<<<<< HEAD
=======
    # html, web, sql, dll
    "html", "css", "js", "py", "java", "php", "sql", "db", "dll",
>>>>>>> afbb9dd (Full body refresh)
}


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# =========================
# Helpers
# =========================
def _get_user_hierarchy(user):
    """Return (organization_id, directorate_id, department_id) for user, best-effort."""
    dept_id = getattr(user, "department_id", None)
    org_id = None
    dir_id = None
    if dept_id:
        dept = Department.query.get(int(dept_id))
        if dept:
            dir_id = dept.directorate_id
            if dir_id:
                d = Directorate.query.get(int(dir_id))
                if d:
                    org_id = d.organization_id
    return org_id, dir_id, dept_id


def _select_template_for(user, request_type_id: int):
    """Pick best WorkflowTemplate by routing rules for this user + request_type."""
    org_id, dir_id, dept_id = _get_user_hierarchy(user)

    rules = (
        WorkflowRoutingRule.query
        .filter_by(request_type_id=request_type_id, is_active=True)
        .all()
    )

    candidates = []
    for r in rules:
        if r.organization_id is not None and r.organization_id != org_id:
            continue
        if r.directorate_id is not None and r.directorate_id != dir_id:
            continue
        if r.department_id is not None and r.department_id != dept_id:
            continue
        candidates.append(r)

    if not candidates:
        return None, None

    # specificity DESC, priority ASC, id DESC
    candidates.sort(key=lambda x: (-x.specificity_score(), int(x.priority or 100), -int(x.id or 0)))
    best = candidates[0]
    return best.template, best


def _is_admin(user) -> bool:
    role = (getattr(user, "role", "") or "").strip().upper()
    return role in ("ADMIN", "SUPER_ADMIN")


def _user_can_act_on_step(user, step: WorkflowInstanceStep) -> bool:
    if _is_admin(user):
        return True

    if step.approver_kind == "USER" and step.approver_user_id:
        return step.approver_user_id == user.id

    if step.approver_kind == "ROLE" and step.approver_role:
        return (user.role or "").strip().lower() == (step.approver_role or "").strip().lower()

    if step.approver_kind == "DEPARTMENT" and step.approver_department_id:
        return (
            user.department_id == step.approver_department_id
            and (user.role or "").strip().lower() == "dept_head"
        )

    return False



def _get_request_files(req: WorkflowRequest):
    """
    Returns a list of ArchivedFile linked to the request.
    Supports RequestAttachment table and (optionally) legacy workflow_request_id on ArchivedFile.
    """
    files = []

    # Preferred: RequestAttachment linking table
    try:
        atts = (
            RequestAttachment.query
            .options(joinedload(RequestAttachment.archived_file))
            .filter_by(request_id=req.id)
            .all()
        )
        for a in atts:
            f = getattr(a, "archived_file", None)
            if f and not getattr(f, "is_deleted", False):
                files.append(f)
        if files:
            return files
    except Exception:
        pass

    # Optional legacy: ArchivedFile.workflow_request_id
    try:
        if hasattr(ArchivedFile, "workflow_request_id"):
            files = (
                ArchivedFile.query
                .filter(
                    ArchivedFile.is_deleted.is_(False),
                    ArchivedFile.workflow_request_id == req.id
                )
                .order_by(ArchivedFile.upload_date.desc())
                .all()
            )
            return files
    except Exception:
        pass

    return []


# =========================
# PDF Report
# =========================
@workflow_bp.route("/<int:request_id>/pdf")
@login_required
def request_pdf(request_id):
    req = WorkflowRequest.query.get_or_404(request_id)

    if not _user_can_view_request(current_user, req):
        abort(403)

    logs = (
        AuditLog.query
        .filter_by(request_id=request_id)
        .order_by(AuditLog.created_at.asc())
        .all()
    )

    attachments = _get_request_files(req)

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
    elements.append(Paragraph("Workflow Request Report", styles["Header"]))
    elements.append(Paragraph(f"<b>Request ID:</b> {req.id}", styles["Normal"]))
    elements.append(Paragraph(f"<b>Title:</b> {req.title or '-'}", styles["Normal"]))
    elements.append(Paragraph(f"<b>Status:</b> {req.status or '-'}", styles["Normal"]))
    elements.append(
        Paragraph(
            f"<b>Generated at:</b> {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC",
            styles["Small"]
        )
    )
    elements.append(Spacer(1, 20))

    elements.append(Paragraph("<b>Attachments</b>", styles["Heading2"]))
    if attachments:
        att_table = [["#", "File Name", "Type", "Size (KB)", "Uploaded"]]
        for i, f in enumerate(attachments, start=1):
            att_table.append([
                i,
                f.original_name,
                f.mime_type or "-",
                round((f.file_size or 0) / 1024, 1),
                (f.upload_date.strftime("%Y-%m-%d") if f.upload_date else "-")
            ])

        elements.append(
            Table(
                att_table,
                colWidths=[30, 180, 80, 70, 80],
                style=TableStyle([
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ])
            )
        )
    else:
        elements.append(Paragraph("No attachments.", styles["Normal"]))

    elements.append(PageBreak())
    elements.append(Paragraph("<b>Workflow Timeline</b>", styles["Heading2"]))

    if logs:
        log_table = [["Date", "Action", "From → To", "Note"]]
        for log in logs:
            log_table.append([
                log.created_at.strftime("%Y-%m-%d %H:%M") if log.created_at else "-",
                log.action or "-",
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
        elements.append(Paragraph("No workflow actions recorded.", styles["Normal"]))

    signed_attachments = [f for f in attachments if getattr(f, "is_signed", False)]
    if signed_attachments:
        elements.append(Spacer(1, 20))
        elements.append(
            Paragraph(
                f"<b>Signed:</b> {len(signed_attachments)} attachment(s) are signed.",
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


# =========================
# Upload attachment to request
# =========================
@workflow_bp.route("/<int:request_id>/upload-attachment", methods=["POST"])
@login_required
def upload_attachment(request_id):
    req = WorkflowRequest.query.get_or_404(request_id)

    if not _user_can_view_request(current_user, req):
        abort(403)

    file = request.files.get("file")
    description = request.form.get("description")

    if not file or not file.filename:
        abort(400)

    original_name = (file.filename or "").strip()
    original_name = os.path.basename(original_name).replace("\x00", "")
    if not original_name or not allowed_file(original_name):
        abort(400)

    if "." not in original_name:
        abort(400)

    ext = original_name.rsplit(".", 1)[1].lower().strip()
    if not ext:
        abort(400)

    stored_name = f"{uuid.uuid4().hex}.{ext}"
    file_path = os.path.join(BASE_STORAGE, stored_name)

    os.makedirs(BASE_STORAGE, exist_ok=True)

    try:
        file.save(file_path)

        archived = ArchivedFile(
            original_name=original_name,
            stored_name=stored_name,
            description=description,
            file_path=file_path,
            mime_type=file.mimetype,
            file_size=os.path.getsize(file_path),
            owner_id=current_user.id,
            visibility="workflow"
        )

        # Optional legacy linkage if exists in your model/db
        if hasattr(archived, "workflow_request_id"):
            setattr(archived, "workflow_request_id", req.id)

        db.session.add(archived)
        db.session.flush()  # get archived.id

        # Preferred linkage table
        try:
            db.session.add(RequestAttachment(
                request_id=req.id,
                archived_file_id=archived.id
            ))
        except Exception:
            pass

        db.session.add(AuditLog(
            request_id=req.id,
            user_id=current_user.id,
            action="WORKFLOW_ATTACHMENT_UPLOADED",
            old_status=None,
            new_status=None,
            note=f"Attachment: {archived.original_name}",
            target_type="ARCHIVE_FILE",
            target_id=archived.id
        ))

        emit_event(
            actor_id=current_user.id,
            action="WORKFLOW_ATTACHMENT_UPLOADED",
            message=f"تم رفع مرفق على الطلب #{req.id}",
            target_type="WORKFLOW_REQUEST",
            target_id=req.id,
            notify_role="ADMIN",
            auto_commit=False
        )

        db.session.commit()

        flash("Attachment uploaded successfully", "success")
        return redirect(url_for("workflow.view_request", request_id=req.id))

    except Exception as e:
        db.session.rollback()
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass

        flash(f"حدث خطأ أثناء رفع المرفق: {e}", "danger")
        return redirect(url_for("workflow.view_request", request_id=req.id))


# =========================
# Download workflow attachment
# =========================
@workflow_bp.route("/attachment/<int:file_id>/download")
@login_required
def download_workflow_attachment(file_id):
    file = ArchivedFile.query.filter(
        ArchivedFile.id == file_id,
        ArchivedFile.is_deleted.is_(False)
    ).first_or_404()

    req = None

    # Optional legacy
    if hasattr(file, "workflow_request_id") and getattr(file, "workflow_request_id", None):
        req = WorkflowRequest.query.get_or_404(file.workflow_request_id)
    else:
        att = RequestAttachment.query.filter_by(archived_file_id=file.id).first()
        if att:
            req = WorkflowRequest.query.get_or_404(att.request_id)

    if not req or not _user_can_view_request(current_user, req):
        abort(403)

    return send_file(
        file.file_path,
        as_attachment=True,
        download_name=file.original_name
    )





# =========================
# Notifications
# =========================
@workflow_bp.route("/notifications")
@login_required
def notifications():
    page = request.args.get("page", 1, type=int)
    per_page = 20

    scope = (request.args.get("scope") or "inbox").strip().lower()
    if scope not in {"inbox", "sent"}:
        scope = "inbox"

    notif_type = request.args.get("type")
    read_state = request.args.get("read")
    role = request.args.get("role")

    date_from = request.args.get("from")   # YYYY-MM-DD
    date_to = request.args.get("to")       # YYYY-MM-DD

    query = (
        Notification.query
        .filter(Notification.user_id == current_user.id)
        .filter(Notification.is_mirror.is_(scope == "sent"))
    )

    if notif_type:
        query = query.filter(Notification.type == notif_type)

    if read_state == "unread":
        query = query.filter(Notification.is_read.is_(False))
    elif read_state == "read":
        query = query.filter(Notification.is_read.is_(True))

    if role:
        query = query.filter(Notification.role == role)

    if date_from:
        start = datetime.strptime(date_from, "%Y-%m-%d")
        query = query.filter(Notification.created_at >= start)

    if date_to:
        end = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
        query = query.filter(Notification.created_at < end)

    pagination = query.order_by(Notification.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )

    # Counts shown in header
    unread_count = (
        Notification.query
        .filter_by(user_id=current_user.id, is_mirror=False, is_read=False)
        .count()
    )
    pending_sent_count = (
        Notification.query
        .filter_by(user_id=current_user.id, is_mirror=True, is_read=False)
        .count()
    )

    return render_template(
        "workflow/notifications.html",
        notifications=pagination.items,
        pagination=pagination,
        unread_count=unread_count,
        pending_sent_count=pending_sent_count,
        scope=scope,
        filters={
            "type": notif_type,
            "read": read_state,
            "role": role,
            "from": date_from,
            "to": date_to,
            "scope": scope,
        }
    )


@workflow_bp.route("/notifications/unread-count")
@login_required
def unread_notifications_count():
    count = Notification.query.filter_by(
        user_id=current_user.id,
        is_mirror=False,
        is_read=False
    ).count()
    return jsonify({"count": count})


def _sync_mirror_for_event(event_key: str):
    """Auto-mark sender mirror notification as read when all recipients have read."""
    if not event_key:
        return

    pending = (
        db.session.query(func.count(Notification.id))
        .filter(
            Notification.event_key == event_key,
            Notification.is_mirror.is_(False),
            Notification.is_read.is_(False)
        )
        .scalar()
    ) or 0

    if int(pending) == 0:
        db.session.execute(
            update(Notification)
            .where(
                Notification.event_key == event_key,
                Notification.is_mirror.is_(True),
                Notification.is_read.is_(False)
            )
            .values(is_read=True)
        )


@workflow_bp.route("/notifications/mark-all-read", methods=["POST"])
@login_required
def mark_all_notifications_read():
    start = time.perf_counter()
    try:
        # Collect event_keys to sync mirrors after bulk update
        event_keys = [
            ek for (ek,) in (
                db.session.query(Notification.event_key)
                .filter(
                    Notification.user_id == current_user.id,
                    Notification.is_mirror.is_(False),
                    Notification.is_read.is_(False),
                    Notification.event_key.isnot(None)
                )
                .distinct()
                .all()
            )
            if ek
        ]

        result = (
            db.session.execute(
                update(Notification)
                .where(
                    Notification.user_id == current_user.id,
                    Notification.is_mirror.is_(False),
                    Notification.is_read.is_(False)
                )
                .values(is_read=True)
            )
        )

        for ek in event_keys:
            _sync_mirror_for_event(ek)

        db.session.commit()

        elapsed = time.perf_counter() - start
        logger.info(
            "Marked %s notifications as read for user_id=%s in %.2fs",
            getattr(result, "rowcount", None),
            current_user.id,
            elapsed
        )
        flash("تم تعليم جميع الإشعارات كمقروءة", "success")
    except Exception:
        db.session.rollback()
        logger.exception("Failed to mark notifications as read for user_id=%s", current_user.id)
        flash("حدث خطأ أثناء تحديث الإشعارات", "danger")

    return redirect(url_for("workflow.notifications"))


@workflow_bp.route("/notifications/<int:notif_id>/read", methods=["POST"])
@login_required
def mark_notification_read(notif_id):
    n = Notification.query.filter_by(
        id=notif_id,
        user_id=current_user.id
    ).first_or_404()

    # Mirror (sent-tracking) notifications are read-only (auto-updated)
    if getattr(n, "is_mirror", False):
        return "", 204

    if not n.is_read:
        n.is_read = True
        if getattr(n, "event_key", None):
            _sync_mirror_for_event(n.event_key)
        db.session.commit()

    return "", 204



@workflow_bp.route("/notifications/stream")
@login_required
def event_stream():
    @stream_with_context
    def gen():
        last = None
        while True:
            try:
                unread = (
                    db.session.query(func.count(Notification.id))
                    .filter(
                        Notification.user_id == current_user.id,
                        Notification.is_mirror.is_(False),
                        Notification.is_read.is_(False)
                    )
                    .scalar()
                ) or 0

                payload = {"unread": int(unread)}

                if payload != last:
                    yield f"data: {json.dumps(payload)}\n\n"
                    last = payload

            except Exception:
                yield "event: ping\ndata: {}\n\n"
            finally:
                db.session.remove()

            time.sleep(6)

    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    return Response(gen(), headers=headers)


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


# =========================
# Helpers: Who can view/act
# =========================
def _user_can_act_on_step(user, step: WorkflowInstanceStep) -> bool:
    # SUPER_ADMIN can act on any step
    if user.has_role("SUPER_ADMIN"):
        return True

    if step.approver_kind == "USER" and step.approver_user_id:
        return step.approver_user_id == user.id

    if step.approver_kind == "ROLE" and step.approver_role:
        # IMPORTANT: SUPER_ADMIN inherits ADMIN
        return user.has_role(step.approver_role)

    if step.approver_kind == "DEPARTMENT" and step.approver_department_id:
        return (
            user.department_id == step.approver_department_id
            and (user.role or "").lower() in ("dept_head", "deputy_head")
        )

    return False

def _user_can_view_request(user, req: WorkflowRequest) -> bool:
    # Owner can always view
    if req.requester_id == user.id:
        return True

    # SUPER_ADMIN can view everything
    if user.has_role("SUPER_ADMIN"):
        return True

    inst = WorkflowInstance.query.filter_by(request_id=req.id).first()
    template = WorkflowTemplate.query.get(inst.template_id) if inst else None

    if not inst:
        return False

    steps = WorkflowInstanceStep.query.filter_by(instance_id=inst.id).all()
    # Admin can view only if he can act on at least one step
    return any(_user_can_act_on_step(user, s) for s in steps)



# =========================
# View request
# =========================
@workflow_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_request():
    # load request types (may be empty)
    request_types = (
        RequestType.query
        .filter_by(is_active=True)
        .order_by(RequestType.name_ar.asc())
        .all()
    )

    # templates always available as manual fallback
    templates = (
        WorkflowTemplate.query
        .filter_by(is_active=True)
        .order_by(WorkflowTemplate.name.asc())
        .all()
    )

    selected_rt_id = request.args.get("request_type_id")
    suggested_template = None
    matched_rule = None

    if selected_rt_id and str(selected_rt_id).isdigit():
        suggested_template, matched_rule = _select_template_for(current_user, int(selected_rt_id))

    if request.method == "POST":
        title = (request.form.get("title") or "").strip() or "طلب جديد"
        description = (request.form.get("description") or "").strip()

        rt_id = (request.form.get("request_type_id") or "").strip()
        template_id = (request.form.get("template_id") or "").strip()

        # validate request type if exists
        request_type_id = None
        if request_types:
            if not rt_id.isdigit():
                flash("يرجى اختيار نوع الطلب.", "danger")
                return redirect(request.url)
            request_type_id = int(rt_id)

        # choose template: manual first, else routing, else single-template fallback
        template = None
        if template_id.isdigit():
            template = WorkflowTemplate.query.get_or_404(int(template_id))
        elif request_type_id:
            template, matched = _select_template_for(current_user, request_type_id)

        # If there is only one active template, auto-select it (common during early setup)
        if not template and templates and len(templates) == 1:
            template = templates[0]

        if not template:
            flash("لا يوجد مسار مناسب. يرجى اختيار مسار (Template) يدويًا أو إضافة Routing Rule.", "danger")
            return redirect(request.url)

        req = WorkflowRequest(
            requester_id=current_user.id,
            status="DRAFT",
            title=title,
            description=description,
            request_type_id=request_type_id
        )
        db.session.add(req)
        db.session.flush()

        # IMPORTANT: single commit per route (engine does not commit)
        start_workflow_for_request(
            req,
            template,
            created_by_user_id=current_user.id,
            auto_commit=False
        )


        # ✅ Notification for requester: request created and workflow started
        try:
            emit_event(
                actor_id=current_user.id,
                action='REQUEST_CREATED',
                message=f"تم إنشاء طلب جديد #{req.id} وبدء المسار: {template.name}",
                target_type='WorkflowRequest',
                target_id=req.id,
                notify_user_id=current_user.id,
                level='WORKFLOW',
                auto_commit=False,
            )
        except Exception:
            # do not block request creation if notification fails
            pass

        db.session.commit()
        flash("تم إنشاء الطلب وبدء مسار العمل.", "success")
        return redirect(url_for("workflow.view_request", request_id=req.id))

    return render_template(
        "workflow/new_request.html",
        request_types=request_types,
        templates=templates,
        selected_rt_id=int(selected_rt_id) if (selected_rt_id and str(selected_rt_id).isdigit()) else None,
        suggested_template=suggested_template,
        matched_rule=matched_rule
    )




# =========================
# Inbox: pending steps for me
# =========================
@workflow_bp.route("/inbox")
@login_required
def inbox():
    """Pending steps for current user (task inbox) with optional search."""
    search = (request.args.get("q") or "").strip()

    user_role_norm = (getattr(current_user, 'role', '') or '').strip().lower().replace('-', '_').replace(' ', '_')


    # SUPER_ADMIN sees all pending current steps
    q = (
        db.session.query(WorkflowRequest, WorkflowInstance, WorkflowInstanceStep)
        .join(WorkflowInstance, WorkflowInstance.request_id == WorkflowRequest.id)
        .join(WorkflowInstanceStep, WorkflowInstanceStep.instance_id == WorkflowInstance.id)
        .filter(WorkflowInstance.is_completed.is_(False))
        .filter(WorkflowInstanceStep.status == "PENDING")
        .filter(WorkflowInstanceStep.step_order == WorkflowInstance.current_step_order)
    )

    # Search (by id/title/description/requester email)
    if search:
        like = f"%{search}%"
        q = q.join(User, User.id == WorkflowRequest.requester_id)
        conds = [
            WorkflowRequest.title.ilike(like),
            WorkflowRequest.description.ilike(like),
            User.email.ilike(like),
        ]
        if search.isdigit():
            try:
                conds.insert(0, WorkflowRequest.id == int(search))
            except Exception:
                pass
        q = q.filter(or_(*conds))

    if not current_user.has_role("SUPER_ADMIN"):
        q = q.filter(
            or_(
                # USER
                db.and_(
                    WorkflowInstanceStep.approver_kind == "USER",
                    WorkflowInstanceStep.approver_user_id == current_user.id,
                ),
                # ROLE
                db.and_(
                    WorkflowInstanceStep.approver_kind == "ROLE",
                    WorkflowInstanceStep.approver_role.ilike(current_user.role),
                ),
                # DEPARTMENT (رئيس دائرة)
                db.and_(
                    WorkflowInstanceStep.approver_kind == "DEPARTMENT",
                    WorkflowInstanceStep.approver_department_id == current_user.department_id,
                    user_role_norm == "dept_head",
                ),
            )
        )

    rows = q.order_by(WorkflowRequest.id.desc()).all()
    return render_template("workflow/inbox.html", rows=rows, q=search)

# =========================
# View Request (Timeline + Action)
# =========================
@workflow_bp.route("/request/<int:request_id>")
@login_required
def view_request(request_id):
    req = WorkflowRequest.query.get_or_404(request_id)

    if not _user_can_view_request(current_user, req):
        flash("غير مصرح لك بمراجعة هذا الطلب", "danger")
        return redirect(url_for("workflow.inbox"))

    # ✅ Mark related WORKFLOW notifications as read when the approver opens the request
    try:
        pending_notifs = (
            Notification.query
            .filter(
                Notification.user_id == current_user.id,
                Notification.is_mirror.is_(False),
                Notification.is_read.is_(False),
                Notification.type == "WORKFLOW",
                Notification.message.contains(f"#{req.id}")
            )
            .all()
        )
        if pending_notifs:
            for n in pending_notifs:
                n.is_read = True
                if n.event_key:
                    _sync_mirror_for_event(n.event_key)
            db.session.commit()
    except Exception:
        db.session.rollback()

    inst = WorkflowInstance.query.filter_by(request_id=req.id).first()
    template = WorkflowTemplate.query.get(inst.template_id) if inst else None


    steps = []
    current_step = None
    can_decide = False

    if inst:
        steps = (
            WorkflowInstanceStep.query
            .filter_by(instance_id=inst.id)
            .order_by(WorkflowInstanceStep.step_order.asc())
            .all()
        )
        current_step = next((s for s in steps if s.step_order == inst.current_step_order), None)
        if current_step and current_step.status == "PENDING":
            can_decide = _user_can_act_on_step(current_user, current_step)

    # Attachments (linked table)
    atts = RequestAttachment.query.filter_by(request_id=req.id).all()
    file_ids = [a.archived_file_id for a in atts]
    files_map = {}
    if file_ids:
        files = ArchivedFile.query.filter(ArchivedFile.id.in_(file_ids)).all()
        files_map = {f.id: f for f in files}

    audit = (
        AuditLog.query
        .filter_by(request_id=req.id)
        .order_by(AuditLog.created_at.desc())
        .limit(200)
        .all()
    )

    # maps for readable routing display
    users_map = {u.id: u for u in User.query.all()}
    depts_map = {d.id: d for d in Department.query.all()}
    dirs_map = {d.id: d for d in Directorate.query.all()}

    return render_template(
        "workflow/view_request.html",
        req=req,
        inst=inst,
        steps=steps,
        current_step=current_step,
        can_decide=can_decide,
        attachments=atts,
        files_map=files_map,
        audit=audit,
        users_map=users_map,
        depts_map=depts_map,
        dirs_map=dirs_map,
        template=template
    )




# =========================
# Delete Request (SUPER_ADMIN only)
# =========================
@workflow_bp.route("/request/<int:request_id>/delete", methods=["POST"])
@login_required
@roles_required("SUPER_ADMIN")
def delete_request(request_id):
    """Hard-delete a request (Super Admin only) while preserving audit trail."""
    req = WorkflowRequest.query.get_or_404(request_id)

    rid = req.id
    requester_id = req.requester_id

    try:
        # Detach previous audit entries so FK won't block deletion
        AuditLog.query.filter(AuditLog.request_id == rid).update(
            {
                AuditLog.request_id: None,
                AuditLog.target_type: "WorkflowRequest",
                AuditLog.target_id: rid,
            },
            synchronize_session=False
        )

        # Delete dependent workflow rows
        Approval.query.filter_by(request_id=rid).delete(synchronize_session=False)
        RequestEscalation.query.filter_by(request_id=rid).delete(synchronize_session=False)
        RequestAttachment.query.filter_by(request_id=rid).delete(synchronize_session=False)

        inst = WorkflowInstance.query.filter_by(request_id=rid).first()
        if inst:
            # WorkflowInstanceStep uses instance_id (no request_id)
            WorkflowInstanceStep.query.filter_by(instance_id=inst.id).delete(synchronize_session=False)
            db.session.delete(inst)

        # Create deletion audit log (without request_id FK)
        db.session.add(AuditLog(
            action="REQUEST_DELETED",
            user_id=current_user.id,
            target_type="WorkflowRequest",
            target_id=rid,
            note=f"Request #{rid} deleted by {current_user.email}"
        ))

        # Notify requester
        if requester_id:
            emit_event(
                actor_id=current_user.id,
                action="REQUEST_DELETED",
                message=f"تم حذف طلبك رقم #{rid} بواسطة الإدارة.",
                target_type="WorkflowRequest",
                target_id=rid,
                notify_user_id=requester_id,
                level="WARNING",
                auto_commit=False
            )

        # Finally delete the request
        db.session.delete(req)
        db.session.commit()

        flash("✅ تم حذف الطلب نهائياً.", "success")
        return redirect(url_for("workflow.inbox"))

    except Exception as e:
        db.session.rollback()
        flash(f"تعذّر حذف الطلب: {e}", "danger")
        return redirect(url_for("workflow.view_request", request_id=rid))

@workflow_bp.route("/request/<int:request_id>/escalate", methods=["GET", "POST"])
@login_required
def escalate_request(request_id):
    req = WorkflowRequest.query.get_or_404(request_id)

    if not _user_can_view_request(current_user, req):
        flash("غير مصرح لك بتصعيد هذا الطلب", "danger")
        return redirect(url_for("workflow.inbox"))

    categories = [
        "SLA_RISK",          # خطر تجاوز SLA
        "URGENT",            # عاجل
        "MISSING_INFO",      # نقص معلومات
        "BLOCKED",           # معيق/متوقف
        "CONFLICT",          # تعارض/خلاف
        "NEED_GUIDANCE",     # بحاجة لتوجيه
        "OTHER",             # أخرى
    ]

    users = User.query.order_by(User.email.asc()).all()

    if request.method == "POST":
        category = (request.form.get("category") or "").strip()
        desc = (request.form.get("description") or "").strip()
        to_user_id = request.form.get("to_user_id")

        try:
            to_user_id = int(to_user_id)
        except Exception:
            to_user_id = None

        if category not in categories:
            flash("اختر نوع تصعيد صحيح.", "danger")
            return redirect(request.url)

        if not to_user_id:
            flash("اختر المستخدم الذي سيتم توجيه التصعيد إليه.", "danger")
            return redirect(request.url)

        if not desc:
            flash("وصف التصعيد مطلوب.", "danger")
            return redirect(request.url)

        to_user = User.query.get(to_user_id)
        if not to_user:
            flash("المستخدم غير موجود.", "danger")
            return redirect(request.url)

        esc = RequestEscalation(
            request_id=req.id,
            from_user_id=current_user.id,
            to_user_id=to_user.id,
            category=category,
            description=desc,
        )
        db.session.add(esc)

        db.session.add(
            AuditLog(
                request_id=req.id,
                user_id=current_user.id,
                action="REQUEST_ESCALATION",
                note=f"Escalation ({category}) to user_id={to_user.id}: {desc[:200]}",
                target_type="WorkflowRequest",
                target_id=req.id,
                created_at=datetime.utcnow(),
            )
        )

        # notify recipient (SSE + badge)
        try:
            emit_event(
                actor_id=current_user.id,
                action="REQUEST_ESCALATION",
                message=f"🚨 تصعيد للطلب #{req.id} ({category}) من {current_user.email}",
                target_type="WorkflowRequest",
                target_id=req.id,
                notify_user_id=to_user.id,
                level="ESCALATION",
                auto_commit=False,
            )
        except Exception:
            pass

        try:
            db.session.commit()
            flash("تم إرسال التصعيد بنجاح.", "success")
            return redirect(url_for("workflow.view_request", request_id=req.id))
        except Exception as e:
            db.session.rollback()
            flash(f"تعذر إرسال التصعيد: {e}", "danger")

    return render_template(
        "workflow/escalate.html",
        req=req,
        users=users,
        categories=categories
    )

# =========================
# Decide Step (Approve/Reject)
# =========================
@workflow_bp.route("/request/<int:request_id>/step/<int:step_order>/decide", methods=["POST"])
@login_required
def decide_request_step(request_id, step_order):
    req = WorkflowRequest.query.get_or_404(request_id)

    if not _user_can_view_request(current_user, req):
        abort(403)

    inst = WorkflowInstance.query.filter_by(request_id=req.id).first()
    if not inst:
        flash("لا يوجد مسار عمل لهذا الطلب.", "warning")
        return redirect(url_for("workflow.view_request", request_id=req.id))

    step = WorkflowInstanceStep.query.filter_by(
        instance_id=inst.id,
        step_order=step_order
    ).first()

    if not step:
        flash("الخطوة غير موجودة.", "danger")
        return redirect(url_for("workflow.view_request", request_id=req.id))

    if not _user_can_act_on_step(current_user, step):
        abort(403)

    decision = (request.form.get("decision") or "").strip().upper()
    note = (request.form.get("note") or "").strip()

    if decision not in ("APPROVED", "REJECTED"):
        flash("قرار غير صالح.", "danger")
        return redirect(url_for("workflow.view_request", request_id=req.id))

    try:
        decide_step(req.id, step_order, current_user.id, decision, note=note, auto_commit=False)
        db.session.commit()
        flash("تم حفظ الإجراء بنجاح.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"خطأ أثناء حفظ الإجراء: {e}", "danger")

    return redirect(url_for("workflow.view_request", request_id=req.id))


# =========================
# Add Note / Comment (without decision)
# =========================
@workflow_bp.route("/request/<int:request_id>/note", methods=["POST"])
@login_required
def add_request_note(request_id):
    req = WorkflowRequest.query.get_or_404(request_id)

    if not _user_can_view_request(current_user, req):
        abort(403)

    note = (request.form.get("note") or "").strip()
    kind = (request.form.get("kind") or "COMMENT").strip().upper()

    if not note:
        flash("يرجى كتابة نص التعليق/الرد.", "warning")
        return redirect(url_for("workflow.view_request", request_id=req.id))

    if kind not in ("COMMENT", "REPLY"):
        kind = "COMMENT"

    try:
        # Always write to AuditLog
        db.session.add(AuditLog(
            request_id=req.id,
            user_id=current_user.id,
            action=f"WORKFLOW_{kind}",
            old_status=req.status,
            new_status=req.status,
            note=note,
            target_type="WorkflowRequest",
            target_id=req.id,
        ))

        actor_label = current_user.email

        if current_user.id == req.requester_id:
            # Requester note -> notify current pending approvers (if any)
            inst = WorkflowInstance.query.filter_by(request_id=req.id).first()
            target_ids = []
            if inst:
                st = WorkflowInstanceStep.query.filter_by(
                    instance_id=inst.id,
                    step_order=inst.current_step_order
                ).first()
                if st and st.status == "PENDING":
                    if st.approver_kind == "USER" and st.approver_user_id:
                        target_ids = [int(st.approver_user_id)]
                    elif st.approver_kind == "ROLE" and st.approver_role:
                        users = User.query.filter(User.role.ilike((st.approver_role or "").strip())).all()
                        target_ids = [u.id for u in users]
                    elif st.approver_kind == "DEPARTMENT" and st.approver_department_id:
                        users = User.query.filter(
                            User.department_id == st.approver_department_id,
                            User.role.ilike("dept_head")
                        ).all()
                        target_ids = [u.id for u in users]

            for uid in set(target_ids):
                emit_event(
                    actor_id=current_user.id,
                    action="WORKFLOW_REQUESTER_NOTE",
                    message=f"رد من مقدم الطلب على الطلب #{req.id}: {note}",
                    target_type="WorkflowRequest",
                    target_id=req.id,
                    notify_user_id=uid,
                    level="WORKFLOW",
                    track_for_actor=True,
                    auto_commit=False,
                )

        else:
            # Reviewer note -> notify requester
            label = "تعليق" if kind == "COMMENT" else "رد"
            emit_event(
                actor_id=current_user.id,
                action="WORKFLOW_NOTE",
                message=f"{label} على طلبك #{req.id} من {actor_label}: {note}",
                target_type="WorkflowRequest",
                target_id=req.id,
                notify_user_id=req.requester_id,
                level="WORKFLOW",
                track_for_actor=True,
                auto_commit=False,
            )

        db.session.commit()
        flash("تم إرسال التعليق/الرد بنجاح.", "success")

    except Exception as e:
        db.session.rollback()
        flash(f"خطأ أثناء إرسال التعليق/الرد: {e}", "danger")

    return redirect(url_for("workflow.view_request", request_id=req.id))
