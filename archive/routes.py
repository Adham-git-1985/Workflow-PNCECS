# archive/routes.py

import os
import uuid
from datetime import datetime, timedelta

from flask import (
    render_template, request, redirect,
    url_for, flash, send_file, abort
)
from flask_login import login_required, current_user
from sqlalchemy import or_, func
from sqlalchemy.orm import joinedload

from extensions import db
from permissions import roles_required

from archive.permissions import (
    can_view_archive_file,
    can_edit_archive_file,
    can_manage_archive_file
)

from archive.cache import get_cached_file, set_cached_file
from archive.queries import archive_access_query
from utils.events import emit_event

from models import (
    ArchivedFile,
    FilePermission,
    User,
    AuditLog,
    WorkflowRequest,
    RequestAttachment,
    WorkflowTemplate,
    SystemSetting,
)

from workflow.engine import start_workflow_for_request

from archive import archive_bp


# =========================
# Storage
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
    # html, web, sql, dll
    "html", "css", "js", "py", "java", "php", "sql", "db", "dll",
}


# =========================
# Helpers
# =========================
def _is_super_admin(user) -> bool:
    try:
        return user.has_role("SUPER_ADMIN") or user.has_role("SUPERADMIN")
    except Exception:
        return False

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_archive_counters(user):
    base = archive_access_query(user)

    total = base.with_entities(func.count(ArchivedFile.id)).scalar()

    mine = base.filter(
        ArchivedFile.owner_id == user.id
    ).with_entities(func.count(ArchivedFile.id)).scalar()

    shared = (
        db.session.query(func.count(FilePermission.file_id))
        .filter(FilePermission.user_id == user.id)
        .scalar()
    )

    return {
        "total": total or 0,
        "mine": mine or 0,
        "shared": shared or 0
    }


def get_shared_count_map(files):
    file_ids = [f.id for f in files]
    if not file_ids:
        return {}

    rows = (
        db.session.query(
            FilePermission.file_id,
            db.func.count(FilePermission.user_id)
        )
        .filter(FilePermission.file_id.in_(file_ids))
        .group_by(FilePermission.file_id)
        .all()
    )

    return {fid: count for fid, count in rows}

def get_shared_by_map(files, current_user_id:int):
    """For each file in list, if it is shared to current_user_id, return who shared it.
    Returns dict: {file_id: sharer_email_or_label}
    """
    file_ids = [f.id for f in files]
    if not file_ids:
        return {}

    # permissions for CURRENT USER only
    rows = (
        db.session.query(FilePermission.file_id, FilePermission.shared_by, User.email)
        .join(User, User.id == FilePermission.shared_by, isouter=True)
        .filter(
            FilePermission.file_id.in_(file_ids),
            FilePermission.user_id == int(current_user_id)
        )
        .all()
    )

    out = {}
    for fid, shared_by, email in rows:
        if shared_by:
            out[int(fid)] = email or f"User#{shared_by}"
        else:
            out[int(fid)] = "غير معروف"
    return out


def get_trash_retention_days() -> int:
    """Return recycle bin retention period in days (SystemSetting TRASH_RETENTION_DAYS)."""
    setting = SystemSetting.query.filter_by(key="TRASH_RETENTION_DAYS").first()
    try:
        return int(setting.value) if setting and setting.value else 30
    except Exception:
        return 30



# =========================
# Sign PDF (flag only)
# =========================
@archive_bp.route("/sign/<int:file_id>", methods=["POST"], endpoint="sign_pdf")
@login_required
def sign_pdf(file_id):
    file = ArchivedFile.query.get_or_404(file_id)

    # Access control: must be able to see the file + have SIGN_ARCHIVE permission.
    if not can_view_archive_file(current_user, file):
        abort(403)

    # Role-based permission (RolePermission). We keep SIGN_ARCHIVE as a permission hook,
    # but it is also seeded as a "basic" permission for active roles on startup.
    try:
        if not current_user.has_role_perm("SIGN_ARCHIVE"):
            abort(403)
    except Exception:
        # Fallback: keep legacy behavior for ADMIN if permission system is unavailable.
        if not current_user.has_role("ADMIN"):
            abort(403)

    if file.is_deleted:
        abort(404)

    # Signing is intended for PDFs only.
    try:
        is_pdf = (getattr(file, "file_type", "") or "").strip().upper() == "PDF"
        if not is_pdf:
            on = (getattr(file, "original_name", "") or "").lower()
            is_pdf = on.endswith(".pdf")
    except Exception:
        is_pdf = False
    if not is_pdf:
        flash("التوقيع متاح لملفات PDF فقط.", "warning")
        return redirect(url_for("archive.file_details", file_id=file.id))

    if file.is_signed:
        flash("الملف موقّع مسبقًا", "warning")
        return redirect(url_for("archive.my_files"))

    file.is_signed = True
    file.signed_at = datetime.utcnow()
    file.signed_by = current_user.id

    db.session.add(
        AuditLog(
            action="ARCHIVE_SIGNED",
            user_id=current_user.id,
            target_type="ARCHIVE_FILE",
            target_id=file.id,
            note=f"File signed: {file.original_name}"
        )
    )

    db.session.commit()

    flash("تم توقيع الملف بنجاح", "success")
    return redirect(url_for("archive.my_files"))


# =========================
# Browse Files
# =========================
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

    pagination = (
        query
        .order_by(ArchivedFile.upload_date.desc())
        .paginate(page=page, per_page=15, error_out=False)
    )

    counters = get_archive_counters(current_user)

    return render_template(
        "archive/files.html",
        files=pagination.items,
        pagination=pagination,
        q=q,
        counters=counters
    )


# =========================
# File Details
# =========================
@archive_bp.route("/files/<int:file_id>")
@login_required
def file_details(file_id):
    page = request.args.get("page", 1, type=int)

    cached_id = get_cached_file(file_id)

    if cached_id:
        file = (
            ArchivedFile.query
            .options(joinedload(ArchivedFile.owner))
            .get_or_404(cached_id)
        )
    else:
        file = (
            ArchivedFile.query
            .options(joinedload(ArchivedFile.owner))
            .get_or_404(file_id)
        )
        set_cached_file(file_id, file.id)

    if getattr(file, 'is_final_deleted', False) and not _is_super_admin(current_user):
        abort(404)

    if not can_view_archive_file(current_user, file):
        abort(403)

    audit_logs = (
        AuditLog.query
        .filter(
            AuditLog.target_id == file.id,
            AuditLog.target_type.in_(["ArchivedFile", "ARCHIVE_FILE"])
        )
        .order_by(AuditLog.created_at.desc())
        .paginate(page=page, per_page=10, error_out=False)
    )

    shared_with = (
        FilePermission.query
        .join(User, User.id == FilePermission.user_id)
        .filter(FilePermission.file_id == file.id)
        .all()
    )

    return render_template(
        "archive/file_details.html",
        file=file,
        audit_logs=audit_logs,
        can_edit=can_edit_archive_file(current_user, file),
        can_manage=can_manage_archive_file(current_user, file),
        shared_with=shared_with
    )


# =========================
# Upload
# =========================
@archive_bp.route("/upload", methods=["GET", "POST"])
@login_required
def upload_file():
    if request.method == "POST":

        # Support multiple file uploads (name="files") while keeping backward compatibility (name="file")
        files = []
        if request.files:
            files = request.files.getlist("files") or []
            if not files:
                single = request.files.get("file")
                if single:
                    files = [single]

        description = request.form.get("description")

        files = [f for f in (files or []) if f and getattr(f, "filename", "")]
        if not files:
            flash("No file selected", "danger")
            return redirect(request.url)

        # Validate all files before saving
        for f in files:
            if not allowed_file(f.filename):
                flash(f"File type not allowed: {f.filename}", "danger")
                return redirect(request.url)

        os.makedirs(BASE_STORAGE, exist_ok=True)

        send_to_workflow = request.form.get("send_to_workflow") == "1"
        template_id = (request.form.get("template_id") or "").strip()

        if send_to_workflow and not template_id.isdigit():
            flash("يرجى اختيار مسار (Template) لبدء مسار العمل.", "danger")
            return redirect(request.url)

        saved_paths = []
        archived_files = []

        try:
            import os as _os
            # 1) Save all files to archive
            for f in files:
                original_name = (f.filename or "").strip()
                original_name = _os.path.basename(original_name).replace("\x00", "")

                if not original_name:
                    raise ValueError("اسم الملف غير صالح")

                if "." not in original_name:
                    raise ValueError(f"الملف بدون امتداد: {original_name}")

                ext = original_name.rsplit(".", 1)[1].lower().strip()
                if not ext:
                    raise ValueError(f"امتداد الملف غير صالح: {original_name}")

                stored_name = f"{uuid.uuid4().hex}.{ext}"
                file_path = os.path.join(BASE_STORAGE, stored_name)

                f.save(file_path)
                saved_paths.append(file_path)

                archived = ArchivedFile(
                    original_name=original_name,
                    stored_name=stored_name,
                    description=description,
                    file_path=file_path,
                    mime_type=f.mimetype,
                    file_size=os.path.getsize(file_path),
                    owner_id=current_user.id,
                    visibility="owner" if not send_to_workflow else "workflow",
                )
                db.session.add(archived)
                db.session.flush()
                archived_files.append(archived)

            # 2) If send to workflow: create ONE request and attach ALL files
            if send_to_workflow:
                req = WorkflowRequest(
                    requester_id=current_user.id,
                    status="DRAFT",
                    title=request.form.get("request_title", "طلب مرفق من الأرشيف"),
                    description=request.form.get("request_description", ""),
                )
                db.session.add(req)
                db.session.flush()

                for archived in archived_files:
                    db.session.add(RequestAttachment(request_id=req.id, archived_file_id=archived.id))
                    # audit attachment so we can display step-aware grouping later
                    db.session.add(AuditLog(
                        request_id=req.id,
                        user_id=current_user.id,
                        action="WORKFLOW_ATTACHMENT_UPLOADED",
                        note=f"Attachment: {archived.original_name} | file_id={archived.id} | step=0 | source=ARCHIVE_UPLOAD",
                        target_type="ARCHIVE_FILE",
                        target_id=archived.id,
                        created_at=datetime.utcnow(),
                    ))

                template = WorkflowTemplate.query.get_or_404(int(template_id))
                start_workflow_for_request(
                    req,
                    template,
                    created_by_user_id=current_user.id,
                    auto_commit=False,
                )

                # notify admins (single)
                emit_event(
                    actor_id=current_user.id,
                    action="ARCHIVE_UPLOADED",
                    message=f"تم رفع {len(archived_files)} ملف/ملفات وبدء مسار عمل للطلب #{req.id}",
                    target_type="WorkflowRequest",
                    target_id=req.id,
                    notify_role="ADMIN",
                    auto_commit=False,
                )

                db.session.commit()
                flash("تم رفع الملفات وبدء مسار العمل بنجاح", "success")
                return redirect(url_for("workflow.view_request", request_id=req.id))

            # 3) Archive only: notify admins for each file (or single message)
            for archived in archived_files:
                emit_event(
                    actor_id=current_user.id,
                    action="ARCHIVE_UPLOADED",
                    message=f"تم رفع ملف أرشيف: {archived.original_name}",
                    target_type="ARCHIVE_FILE",
                    target_id=archived.id,
                    notify_role="ADMIN",
                    auto_commit=False,
                )

            db.session.commit()
            flash("File(s) uploaded successfully", "success")
            return redirect(url_for("archive.my_files"))

        except Exception as e:
            db.session.rollback()
            # cleanup saved files
            for fp in saved_paths:
                try:
                    if os.path.exists(fp):
                        os.remove(fp)
                except Exception:
                    pass

            flash(f"حدث خطأ أثناء رفع الملف: {e}", "danger")
            return redirect(request.url)


    templates = (
        WorkflowTemplate.query
        .filter_by(is_active=True)
        .order_by(WorkflowTemplate.id.desc())
        .all()
    )
    return render_template("archive/upload.html", templates=templates)


# =========================
# My Files
# =========================
@archive_bp.route("/my-files")
@login_required
def my_files():
    q = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)
    per_page = 15

    query = archive_access_query(current_user)

    if q:
        query = query.filter(
            or_(
                ArchivedFile.original_name.ilike(f"%{q}%"),
                ArchivedFile.description.ilike(f"%{q}%")
            )
        )

    pagination = (
        query
        .order_by(ArchivedFile.upload_date.desc())
        .paginate(page=page, per_page=per_page, error_out=False)
    )

    delegated_files = {
        p.file_id
        for p in FilePermission.query.filter(
            FilePermission.user_id == current_user.id,
            FilePermission.delegated_by.isnot(None)
        )
    }

    counters = get_archive_counters(current_user)
    shared_count = get_shared_count_map(pagination.items)
    shared_by_map = get_shared_by_map(pagination.items, current_user.id)

    return render_template(
        "archive/my_files.html",
        files=pagination.items,
        pagination=pagination,
        q=q,
        delegated_files=delegated_files,
        counters=counters,
        shared_count=shared_count,
        shared_by_map=shared_by_map
    )




# =========================
# Shared By Details
# =========================
@archive_bp.route("/shared-by/<int:file_id>")
@login_required
def shared_by(file_id):
    file = (
        archive_access_query(current_user)
        .filter(ArchivedFile.id == file_id)
        .first()
    )

    if not file or file.is_deleted:
        abort(404)

    perms = []
    if current_user.has_role("ADMIN") or current_user.id == file.owner_id:
        perms = (
            FilePermission.query
            .options(joinedload(FilePermission.user))
            .filter(FilePermission.file_id == file.id)
            .all()
        )
    else:
        perm = FilePermission.query.filter_by(file_id=file.id, user_id=current_user.id).first()
        if not perm:
            abort(403)
        perms = [perm]

    # Resolve sharers
    sharer_ids = {p.shared_by for p in perms if getattr(p, "shared_by", None)}
    sharers = {}
    if sharer_ids:
        for u in User.query.filter(User.id.in_(list(sharer_ids))).all():
            sharers[int(u.id)] = u

    return render_template(
        "archive/shared_by.html",
        file=file,
        perms=perms,
        sharers=sharers,
    )
# =========================
# Download
# =========================
@archive_bp.route("/download/<int:file_id>")
@login_required
def download_file(file_id):
    file = (
        archive_access_query(current_user)
        .filter(ArchivedFile.id == file_id)
        .first()
    )

    if not file or file.is_deleted:
        abort(403)

    perm = FilePermission.query.filter_by(
        file_id=file.id,
        user_id=current_user.id
    ).first()

    if perm and not perm.can_download:
        abort(403)

    return send_file(
        file.file_path,
        as_attachment=True,
        download_name=file.original_name
    )


# =========================
# Preview (inline)
# =========================
@archive_bp.route("/preview/<int:file_id>")
@login_required
def preview_file(file_id):
    file = (
        archive_access_query(current_user)
        .filter(ArchivedFile.id == file_id)
        .first()
    )

    if not file or file.is_deleted:
        abort(404)

    perm = FilePermission.query.filter_by(
        file_id=file.id,
        user_id=current_user.id
    ).first()

    if perm and not perm.can_download:
        abort(403)

    return send_file(
        file.file_path,
        mimetype=file.mime_type or "application/octet-stream",
        as_attachment=False,
        download_name=file.original_name,
        conditional=True
    )


# =========================
# Share / Delegate
# =========================
@archive_bp.route("/share/<int:file_id>", methods=["GET", "POST"])
@login_required
def share_file(file_id):
    file = (
        archive_access_query(current_user)
        .filter(ArchivedFile.id == file_id)
        .first()
    )

    if not file or file.is_deleted:
        abort(403)

    is_delegated_user = (
        not current_user.has_role("ADMIN")
        and file.owner_id != current_user.id
    )

    can_delegate = (
        current_user.has_role("ADMIN")
        or file.owner_id == current_user.id
        or FilePermission.query.filter_by(
            file_id=file.id,
            user_id=current_user.id,
            can_share=True
        ).first()
    )

    if not can_delegate:
        abort(403)

    users = User.query.filter(User.id != file.owner_id).all()

    shared_user_ids = [
        p.user_id
        for p in FilePermission.query.filter_by(file_id=file.id).all()
    ]

    if request.method == "POST":
        selected_users = request.form.getlist("users")

        if current_user.has_role("ADMIN") or file.owner_id == current_user.id:
            FilePermission.query.filter_by(file_id=file.id).delete()
        else:
            FilePermission.query.filter_by(
                file_id=file.id,
                user_id=current_user.id
            ).delete()

        for uid in selected_users:
            if is_delegated_user and request.form.get(f"can_share_{uid}") == "1":
                db.session.add(
                    AuditLog(
                        action="ARCHIVE_SHARE_DENIED",
                        user_id=current_user.id,
                        target_type="ARCHIVE_FILE",
                        target_id=file.id,
                        note="Delegated user attempted to re-delegate sharing"
                    )
                )
                continue

            expires_raw = request.form.get(f"expires_at_{uid}")
            expires_at = (
                datetime.strptime(expires_raw, "%Y-%m-%d")
                if expires_raw else None
            )

            permission = FilePermission(
                file_id=file.id,
                user_id=int(uid),
                can_download=(request.form.get(f"can_download_{uid}") == "1"),
                can_share=(
                    not is_delegated_user
                    and request.form.get(f"can_share_{uid}") == "1"
                ),
                delegated_by=current_user.id if is_delegated_user else None,
                shared_by=current_user.id,
                expires_at=expires_at
            )

            db.session.add(permission)

            db.session.add(
                AuditLog(
                    action="ARCHIVE_SHARE",
                    user_id=current_user.id,
                    target_type="ARCHIVE_FILE",
                    target_id=file.id,
                    note=(
                        f"Shared with user {uid} | "
                        f"download={permission.can_download} | "
                        f"can_share={permission.can_share} | "
                        f"expires={permission.expires_at}"
                    )
                )
            )

        file.visibility = "shared"

        # Notify each shared user with full details
        for uid in selected_users:
            try:
                uid_int = int(uid)
            except Exception:
                continue

            perm = FilePermission.query.filter_by(file_id=file.id, user_id=uid_int).first()
            if not perm:
                continue

            expires_label = perm.expires_at.strftime("%Y-%m-%d") if perm.expires_at else "بدون"
            emit_event(
                actor_id=current_user.id,
                action="ARCHIVE_FILE_SHARED",
                message=(
                    f"تمت مشاركة ملف معك: {file.original_name} (ID: {file.id}) | "
                    f"من: {current_user.email} | "
                    f"تحميل={'نعم' if perm.can_download else 'لا'} | "
                    f"مشاركة={'نعم' if perm.can_share else 'لا'} | "
                    f"انتهاء={expires_label}"
                ),
                target_type="ARCHIVE_FILE",
                target_id=file.id,
                notify_user_id=uid_int,
                level="INFO",
                track_for_actor=True,
                auto_commit=False,
            )

        db.session.commit()

        flash("File shared successfully", "success")
        return redirect(url_for("archive.my_files"))

    return render_template(
        "archive/share.html",
        file=file,
        users=users,
        shared_user_ids=shared_user_ids
    )


# =========================
# Delete / Restore / Audit
# =========================
@archive_bp.route("/delete/<int:file_id>", methods=["POST"])
@login_required
def delete_file(file_id):
    file = ArchivedFile.query.get_or_404(file_id)

    # Only the uploader (owner) or ADMIN can delete
    if not (current_user.has_role("ADMIN") or file.owner_id == current_user.id):
        abort(403)

    # Safety rules
    if file.is_deleted:
        flash("الملف محذوف مسبقًا", "warning")
        return redirect(url_for("archive.my_files"))

    # Do not allow deleting signed files
    if getattr(file, "is_signed", False):
        flash("لا يمكن حذف ملف موقّع.", "danger")
        return redirect(url_for("archive.file_details", file_id=file.id))

    # Do not allow deleting files attached to workflow requests
    attached = RequestAttachment.query.filter_by(archived_file_id=file.id).first()
    if attached:
        flash("لا يمكن حذف ملف مرتبط بطلب/مسار عمل. قم بإزالة الربط أولاً.", "danger")
        return redirect(url_for("archive.file_details", file_id=file.id))

    # If file is shared with others, owner must unshare first (admin can override if needed)
    shared_count = FilePermission.query.filter_by(file_id=file.id).count()
    if shared_count > 0 and not current_user.has_role("ADMIN"):
        flash("لا يمكن حذف ملف تمت مشاركته. قم بإلغاء المشاركة أولاً.", "danger")
        return redirect(url_for("archive.file_details", file_id=file.id))

    file.is_deleted = True
    file.deleted_at = datetime.utcnow()
    file.deleted_by = current_user.id

    db.session.add(
        AuditLog(
            action="ARCHIVE_DELETE",
            user_id=current_user.id,
            target_type="ARCHIVE_FILE",
            target_id=file.id,
            note=f"File '{file.original_name}' soft deleted"
        )
    )

    db.session.commit()
    flash("File moved to Recycle Bin", "warning")
    return redirect(url_for("archive.my_files"))


@archive_bp.route("/recycle-bin")
@login_required
def recycle_bin():
    q = ArchivedFile.query.filter(
        ArchivedFile.is_deleted.is_(True),
        (ArchivedFile.is_final_deleted.is_(False) | ArchivedFile.is_final_deleted.is_(None))
    )

    if not current_user.has_role("ADMIN"):
        q = q.filter(ArchivedFile.owner_id == current_user.id)


    files = q.order_by(ArchivedFile.deleted_at.desc()).all()

    return render_template(
        "archive/recycle_bin.html",
        files=files,
        trash_retention_days=get_trash_retention_days(),
    )


# =========================
# Super Trash (Final Deleted)
# =========================
@archive_bp.route("/super-trash")
@login_required
def super_trash():
    """List final-deleted files. Visible only to SUPER_ADMIN."""
    if not _is_super_admin(current_user):
        abort(403)

    q = (request.args.get("q") or "").strip()
    page = request.args.get("page", 1, type=int)

    query = (
        ArchivedFile.query
        .options(joinedload(ArchivedFile.owner))
        .filter(ArchivedFile.is_final_deleted.is_(True))
    )

    if q:
        query = query.filter(
            or_(
                ArchivedFile.original_name.ilike(f"%{q}%"),
                ArchivedFile.description.ilike(f"%{q}%")
            )
        )

    pagination = (
        query
        .order_by(ArchivedFile.final_deleted_at.desc(), ArchivedFile.deleted_at.desc())
        .paginate(page=page, per_page=20, error_out=False)
    )

    return render_template(
        "archive/super_trash.html",
        files=pagination.items,
        pagination=pagination,
        q=q,
    )


@archive_bp.route("/super-trash/download/<int:file_id>")
@login_required
def super_trash_download(file_id):
    if not _is_super_admin(current_user):
        abort(403)

    f = ArchivedFile.query.get_or_404(file_id)
    if not getattr(f, "is_final_deleted", False):
        abort(404)

    if not f.file_path or not os.path.exists(f.file_path):
        flash("ملف التخزين غير موجود على القرص.", "danger")
        return redirect(url_for("archive.super_trash"))

    return send_file(
        f.file_path,
        as_attachment=True,
        download_name=f.original_name,
        mimetype=f.mime_type or "application/octet-stream",
    )


@archive_bp.route("/super-trash/preview/<int:file_id>")
@login_required
def super_trash_preview(file_id):
    if not _is_super_admin(current_user):
        abort(403)

    f = ArchivedFile.query.get_or_404(file_id)
    if not getattr(f, "is_final_deleted", False):
        abort(404)

    if not f.file_path or not os.path.exists(f.file_path):
        flash("ملف التخزين غير موجود على القرص.", "danger")
        return redirect(url_for("archive.super_trash"))

    # inline preview
    return send_file(
        f.file_path,
        as_attachment=False,
        download_name=f.original_name,
        mimetype=f.mime_type or "application/octet-stream",
    )


@archive_bp.route("/super-trash/restore-to-bin/<int:file_id>", methods=["POST"])
@login_required
def super_trash_restore_to_bin(file_id):
    if not _is_super_admin(current_user):
        abort(403)

    f = ArchivedFile.query.get_or_404(file_id)
    if not getattr(f, "is_final_deleted", False):
        abort(404)

    f.is_final_deleted = False
    f.final_deleted_at = None
    f.final_deleted_by = None

    # keep it in recycle bin
    f.is_deleted = True
    if not f.deleted_at:
        f.deleted_at = datetime.utcnow()

    db.session.add(
        AuditLog(
            action="ARCHIVE_SUPERTRASH_RESTORE_TO_BIN",
            user_id=current_user.id,
            target_type="ARCHIVE_FILE",
            target_id=f.id,
            note=f"File '{f.original_name}' restored from Super Trash to Recycle Bin"
        )
    )

    db.session.commit()
    flash("✅ تم استعادة الملف إلى سلة المحذوفات.", "success")
    return redirect(url_for("archive.super_trash"))


@archive_bp.route("/super-trash/restore-active/<int:file_id>", methods=["POST"])
@login_required
def super_trash_restore_active(file_id):
    if not _is_super_admin(current_user):
        abort(403)

    f = ArchivedFile.query.get_or_404(file_id)
    if not getattr(f, "is_final_deleted", False):
        abort(404)

    f.is_final_deleted = False
    f.final_deleted_at = None
    f.final_deleted_by = None

    # restore active
    f.is_deleted = False
    f.deleted_at = None
    f.deleted_by = None

    db.session.add(
        AuditLog(
            action="ARCHIVE_SUPERTRASH_RESTORE_ACTIVE",
            user_id=current_user.id,
            target_type="ARCHIVE_FILE",
            target_id=f.id,
            note=f"File '{f.original_name}' restored from Super Trash to Active"
        )
    )

    db.session.commit()
    flash("✅ تم استعادة الملف إلى الملفات النشطة.", "success")
    return redirect(url_for("archive.super_trash"))

@archive_bp.route("/recycle-bin/purge", methods=["POST"])
@login_required
def purge_recycle_bin():
    """Move expired recycle-bin files to Super Trash (final delete).

    Behavior:
    - Files disappear from users/admin lists once final-deleted.
    - Files remain accessible only to SUPER_ADMIN via /archive/super-trash.
    - Files linked to workflow requests are never final-deleted.
    """

    if not current_user.has_role("ADMIN"):
        abort(403)

    days = get_trash_retention_days()
    cutoff = datetime.utcnow() - timedelta(days=days)

    candidates = (
        ArchivedFile.query
        .filter(
            ArchivedFile.is_deleted.is_(True),
            (ArchivedFile.is_final_deleted.is_(False) | ArchivedFile.is_final_deleted.is_(None)),
            ArchivedFile.deleted_at.isnot(None),
            ArchivedFile.deleted_at < cutoff
        )
        .order_by(ArchivedFile.deleted_at.asc())
        .all()
    )

    moved = 0
    skipped = 0

    for f in candidates:
        attached = RequestAttachment.query.filter_by(archived_file_id=f.id).first()
        if attached:
            skipped += 1
            continue

        # Remove sharing permissions first
        FilePermission.query.filter_by(file_id=f.id).delete(synchronize_session=False)

        f.is_final_deleted = True
        f.final_deleted_at = datetime.utcnow()
        f.final_deleted_by = current_user.id

        db.session.add(
            AuditLog(
                action="ARCHIVE_FINAL_DELETE_RETENTION",
                user_id=current_user.id,
                target_type="ARCHIVE_FILE",
                target_id=f.id,
                note=f"File '{f.original_name}' moved to Super Trash (retention {days} days)"
            )
        )

        moved += 1

    db.session.commit()

    if moved:
        flash(f"✅ تم نقل {moved} ملف/ملفات إلى سلة السوبر أدمن (سياسة الاحتفاظ {days} يوم)", "success")
    else:
        flash("لا توجد ملفات منتهية للنقل إلى سلة السوبر أدمن حاليًا.", "info")

    if skipped:
        flash(f"⚠️ تم تجاوز {skipped} ملف لأنه مرتبط بطلب/مسار عمل.", "warning")

    return redirect(url_for("archive.recycle_bin"))


@archive_bp.route("/recycle-bin/purge/<int:file_id>", methods=["POST"])
@login_required
def purge_single_from_recycle_bin(file_id):
    """Move a single file from recycle bin to Super Trash (final delete).

    Allowed for ADMIN or the file owner.
    Safety rules:
      - Must be already soft-deleted (is_deleted=True)
      - Cannot final-delete files linked to workflow requests
      - If shared, owner must unshare first (ADMIN can override)

    NOTE: This does NOT delete the DB record or the physical file.
    """

    f = ArchivedFile.query.get_or_404(file_id)

    if not (current_user.has_role("ADMIN") or f.owner_id == current_user.id):
        abort(403)

    if not f.is_deleted:
        flash("الملف ليس في سلة المحذوفات.", "warning")
        return redirect(url_for("archive.file_details", file_id=f.id))

    if getattr(f, 'is_final_deleted', False):
        flash("هذا الملف موجود بالفعل في سلة السوبر أدمن.", "info")
        return redirect(url_for("archive.recycle_bin"))

    # Never final-delete files linked to workflow requests
    attached = RequestAttachment.query.filter_by(archived_file_id=f.id).first()
    if attached:
        flash("لا يمكن حذف الملف نهائيًا لأنه مرتبط بطلب/مسار عمل.", "danger")
        return redirect(url_for("archive.recycle_bin"))

    # If shared with others, owner must unshare first (admin can override)
    shared_count = FilePermission.query.filter_by(file_id=f.id).count()
    if shared_count > 0 and not current_user.has_role("ADMIN"):
        flash("لا يمكن حذف ملف تمت مشاركته. قم بإلغاء المشاركة أولاً.", "danger")
        return redirect(url_for("archive.recycle_bin"))

    # Remove sharing permissions first
    FilePermission.query.filter_by(file_id=f.id).delete(synchronize_session=False)

    f.is_final_deleted = True
    f.final_deleted_at = datetime.utcnow()
    f.final_deleted_by = current_user.id

    db.session.add(
        AuditLog(
            action="ARCHIVE_FINAL_DELETE_SINGLE",
            user_id=current_user.id,
            target_type="ARCHIVE_FILE",
            target_id=f.id,
            note=f"File '{f.original_name}' moved to Super Trash (manual final delete)"
        )
    )

    db.session.commit()
    flash("✅ تم نقل الملف إلى سلة السوبر أدمن (حذف نهائي).", "success")
    return redirect(url_for("archive.recycle_bin"))


@archive_bp.route("/restore/<int:file_id>")
@login_required
def restore_file(file_id):
    file = ArchivedFile.query.get_or_404(file_id)

    if getattr(file, 'is_final_deleted', False):
        abort(404)

    if not (current_user.has_role("ADMIN") or file.owner_id == current_user.id):
        abort(403)

    file.is_deleted = False
    file.deleted_at = None
    file.deleted_by = None

    db.session.add(
        AuditLog(
            action="ARCHIVE_RESTORE",
            user_id=current_user.id,
            target_type="ARCHIVE_FILE",
            target_id=file.id,
            note=f"File '{file.original_name}' restored"
        )
    )

    db.session.commit()
    flash("File restored successfully", "success")
    return redirect(url_for("archive.recycle_bin"))


@archive_bp.route("/audit-log")
@login_required
def archive_audit_log():
    if not current_user.has_role("ADMIN"):
        abort(403)

    logs = AuditLog.query.filter(
        AuditLog.action.in_([
            "ARCHIVE_DELETE",
            "ARCHIVE_RESTORE",
            "ARCHIVE_SHARE",
            "ARCHIVE_UNSHARE",
            "ARCHIVE_SIGNED",
            "ARCHIVE_SHARE_DENIED"
        ])
    ).order_by(
        AuditLog.created_at.desc()
    ).all()

    return render_template(
        "archive/audit_log.html",
        logs=logs
    )


@archive_bp.route("/unshare/<int:permission_id>", methods=["POST"])
@login_required
def unshare_file(permission_id):
    permission = FilePermission.query.get_or_404(permission_id)
    file = ArchivedFile.query.get_or_404(permission.file_id)

    if not (
        current_user.has_role("ADMIN")
        or file.owner_id == current_user.id
        or permission.delegated_by == current_user.id
    ):
        abort(403)

    target_user = User.query.get(permission.user_id)

    db.session.delete(permission)

    db.session.add(
        AuditLog(
            action="ARCHIVE_UNSHARE",
            user_id=current_user.id,
            target_type="ARCHIVE_FILE",
            target_id=file.id,
            note=f"تم إلغاء مشاركة الملف مع المستخدم {target_user.email}"
        )
    )

    db.session.commit()

    flash("تم إلغاء المشاركة بنجاح", "success")
    return redirect(url_for("archive.file_details", file_id=file.id))


@archive_bp.route("/shared-files")
@login_required
def shared_files():
    if not current_user.has_role("ADMIN"):
        abort(403)

    permissions = (
        FilePermission.query
        .join(ArchivedFile, ArchivedFile.id == FilePermission.file_id)
        .join(User, User.id == FilePermission.user_id)
        .order_by(FilePermission.id.desc())
        .all()
    )

    return render_template(
        "archive/shared_files.html",
        permissions=permissions
    )
