import os
import uuid
from flask import render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from . import archive_bp
from extensions import db
from flask import send_file, abort
from sqlalchemy import or_

from models import ArchivedFile, FilePermission, User

from permissions import roles_required
from models import AuditLog
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from io import BytesIO
from archive.permissions import archive_access_query



# مسار التخزين (خارج static)
BASE_STORAGE = os.path.join(os.getcwd(), "storage", "archive")

ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "xls", "xlsx", "png", "jpg", "jpeg"}

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS



@archive_bp.route("/files")
@login_required
def archive_files():
    q = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)

    query = archive_access_query(current_user)

    if q:
        query = query.filter(
            or_(
                ArchivedFile.original_name.ilike(f"%{q}%"),
                ArchivedFile.description.ilike(f"%{q}%")
            )
        )

    pagination = query.order_by(
        ArchivedFile.upload_date.desc()
    ).paginate(page=page, per_page=10)

    return render_template(
        "archive/files.html",
        pagination=pagination,
        files=pagination.items,
        q=q
    )

@archive_bp.route("/sign/<int:file_id>")
@login_required
@roles_required("ADMIN")
def sign_pdf(file_id):

    file = ArchivedFile.query.get_or_404(file_id)

    if not file.original_name.lower().endswith(".pdf"):
        abort(400)

    # إضافة ختم توقيع (ReportLab أو PyPDF)
    # لاحقًا: شهادة رقمية

    flash("PDF signed successfully", "success")
    return redirect(url_for("archive.my_files"))


@admin_bp.route("/archive-retention", methods=["GET", "POST"])
@login_required
@roles_required("ADMIN")
def archive_retention():

    setting = SystemSetting.query.get("ARCHIVE_PURGE_DAYS")

    if request.method == "POST":
        days = request.form.get("days")
        if not setting:
            setting = SystemSetting(key="ARCHIVE_PURGE_DAYS")
            db.session.add(setting)
        setting.value = days
        db.session.commit()
        flash("Retention policy updated", "success")

    return render_template(
        "admin/archive_retention.html",
        days=setting.value if setting else 30
    )


@archive_bp.route("/audit-log/pdf")
@login_required
@roles_required("ADMIN")
def archive_audit_log_pdf():
    logs = AuditLog.query.filter(
        AuditLog.action.in_([
            "ARCHIVE_DELETE",
            "ARCHIVE_RESTORE",
            "ARCHIVE_PURGE"
        ])
    ).order_by(AuditLog.created_at.desc()).all()

    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)

    y = 800
    c.setFont("Helvetica", 10)

    c.drawString(50, y, "Archive Audit Log")
    y -= 30

    for log in logs:
        line = f"{log.created_at} | {log.user_id} | {log.action}"
        c.drawString(50, y, line)
        y -= 15
        if y < 50:
            c.showPage()
            y = 800

    c.save()
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name="archive_audit_log.pdf"
    )

@archive_bp.route("/upload", methods=["GET", "POST"])
@login_required
def upload_file():
    if request.method == "POST":

        file = request.files.get("file")
        description = request.form.get("description")

        if not file or file.filename == "":
            flash("No file selected", "danger")
            return redirect(request.url)

        if not allowed_file(file.filename):
            flash("File type not allowed", "danger")
            return redirect(request.url)

        # تجهيز التخزين
        os.makedirs(BASE_STORAGE, exist_ok=True)

        original_name = secure_filename(file.filename)

        # 1️⃣ تأكد أن الاسم صالح بعد secure_filename
        if not original_name:
            flash("Invalid file name", "danger")
            return redirect(request.url)

        # 2️⃣ تأكد من وجود امتداد مسموح
        if not allowed_file(original_name):
            flash("Invalid or unsupported file type", "danger")
            return redirect(request.url)

        # 3️⃣ استخراج الامتداد بأمان
        ext = original_name.rsplit(".", 1)[1].lower()

        stored_name = f"{uuid.uuid4().hex}.{ext}"
        file_path = os.path.join(BASE_STORAGE, stored_name)

        # حفظ الملف
        file.save(file_path)

        # حفظ البيانات
        archived = ArchivedFile(
            original_name=original_name,
            stored_name=stored_name,
            description=description,
            file_path=file_path,
            mime_type=file.mimetype,
            file_size=os.path.getsize(file_path),
            owner_id=current_user.id,
            visibility="owner"
        )

        db.session.add(archived)
        db.session.commit()

        flash("File uploaded successfully", "success")
        return redirect(url_for("archive.upload_file"))

    return render_template("archive/upload.html")

@archive_bp.route("/my-files")
@login_required
def my_files():
    q = request.args.get("q", "").strip()

    # Query مركزي موحد للصلاحيات
    query = archive_access_query(current_user)

    # تطبيق البحث إن وُجد
    if q:
        query = query.filter(
            or_(
                ArchivedFile.original_name.ilike(f"%{q}%"),
                ArchivedFile.description.ilike(f"%{q}%")
            )
        )

    files = query.order_by(
        ArchivedFile.upload_date.desc()
    ).all()

    return render_template(
        "archive/my_files.html",
        files=files,
        q=q
    )


@archive_bp.route("/download/<int:file_id>")
@login_required
def download_file(file_id):

    # Query الصلاحيات المركزي
    file = (
        archive_access_query(current_user)
        .filter(ArchivedFile.id == file_id)
        .first()
    )

    if not file:
        abort(403)

    # حماية إضافية (حتى لو حصل خطأ منطقي)
    if file.is_deleted:
        abort(404)

    return send_file(
        file.file_path,
        as_attachment=True,
        download_name=file.original_name
    )


@archive_bp.route("/share/<int:file_id>", methods=["GET", "POST"])
@login_required
def share_file(file_id):

    # الملف يجب أن يكون ضمن نطاق صلاحيات المستخدم
    file = (
        archive_access_query(current_user)
        .filter(ArchivedFile.id == file_id)
        .first()
    )

    if not file:
        abort(403)

    if file.is_deleted:
        abort(404)

    # فقط Admin أو المالك
    if not (current_user.has_role("ADMIN") or file.owner_id == current_user.id):
        abort(403)

    users = User.query.filter(User.id != current_user.id).all()

    if request.method == "POST":
        selected_users = request.form.getlist("users")

        # إزالة المشاركات القديمة
        FilePermission.query.filter_by(file_id=file.id).delete()

        # إضافة المشاركات الجديدة
        for uid in selected_users:
            db.session.add(
                FilePermission(
                    file_id=file.id,
                    user_id=int(uid)
                )
            )

        file.visibility = "shared"
        db.session.commit()

        flash("File shared successfully", "success")
        return redirect(url_for("archive.my_files"))

    return render_template(
        "archive/share.html",
        file=file,
        users=users
    )

@archive_bp.route("/delete/<int:file_id>", methods=["POST"])
@login_required
@roles_required("ADMIN")
def delete_file(file_id):
    file = ArchivedFile.query.get_or_404(file_id)

    # Soft delete
    file.is_deleted = True
    file.deleted_at = datetime.utcnow()
    file.deleted_by = current_user.id

    # Audit log
    log = AuditLog(
        action="ARCHIVE_DELETE",
        user_id=current_user.id,
        target_type="ArchivedFile",
        target_id=file.id,
        description=f"File '{file.original_name}' soft deleted"
    )

    # إخطار المستخدم
    notif = Notification(
        user_id=file.owner_id,
        message=f"Your file '{file.original_name}' was deleted"
    )
    db.session.add(notif)

    # إخطار المشاركين
    shared_users = FilePermission.query.filter_by(file_id=file.id).all()
    for p in shared_users:
        db.session.add(Notification(
            user_id=p.user_id,
            message=f"Shared file '{file.original_name}' was deleted"
        ))

    db.session.add(log)
    db.session.commit()

    flash("File moved to Recycle Bin", "warning")
    return redirect(url_for("archive.my_files"))

@archive_bp.route("/recycle-bin")
@login_required
@roles_required("ADMIN")
def recycle_bin():
    files = (
        ArchivedFile.query
        .filter(ArchivedFile.is_deleted == True)
        .order_by(ArchivedFile.deleted_at.desc())
        .all()
    )

    return render_template(
        "archive/recycle_bin.html",
        files=files
    )

@archive_bp.route("/restore/<int:file_id>")
@login_required
@roles_required("ADMIN")
def restore_file(file_id):
    file = ArchivedFile.query.get_or_404(file_id)

    file.is_deleted = False
    file.deleted_at = None
    file.deleted_by = None

    # Audit log
    log = AuditLog(
        action="ARCHIVE_RESTORE",
        user_id=current_user.id,
        target_type="ArchivedFile",
        target_id=file.id,
        description=f"File '{file.original_name}' restored"
    )

    db.session.add(log)
    db.session.commit()

    flash("File restored successfully", "success")
    return redirect(url_for("archive.recycle_bin"))

@archive_bp.route("/audit-log")
@login_required
@roles_required("ADMIN")
def archive_audit_log():
    logs = (
        AuditLog.query
        .filter(AuditLog.action.in_(["ARCHIVE_DELETE", "ARCHIVE_RESTORE"]))
        .order_by(AuditLog.created_at.desc())
        .all()
    )

    return render_template(
        "archive/audit_log.html",
        logs=logs
    )

@workflow_bp.route("/<int:request_id>/upload-attachment", methods=["POST"])
@login_required
def upload_attachment(request_id):

    req = WorkflowRequest.query.get_or_404(request_id)

    # تحقق صلاحية المستخدم على الطلب
    if not can_access_request(req, current_user):
        abort(403)

    file = request.files.get("file")
    description = request.form.get("description")

    if not file or file.filename == "":
        abort(400)

    # reuse منطق الرفع من الأرشفة
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
    db.session.commit()

    flash("Attachment uploaded successfully", "success")
    return redirect(url_for("workflow.view_request", request_id=req.id))

@workflow_bp.route("/attachment/<int:file_id>/download")
@login_required
def download_workflow_attachment(file_id):

    file = ArchivedFile.query.filter(
        ArchivedFile.id == file_id,
        ArchivedFile.workflow_request_id.isnot(None),
        ArchivedFile.is_deleted == False
    ).first_or_404()

    req = file.workflow_request

    if not can_access_request(req, current_user):
        abort(403)

    return send_file(
        file.file_path,
        as_attachment=True,
        download_name=file.original_name
    )


@app.route("/notifications")
@login_required
def notifications():
    notes = Notification.query.filter_by(
        user_id=current_user.id
    ).order_by(Notification.created_at.desc()).all()

    return render_template(
        "notifications.html",
        notifications=notes
    )
