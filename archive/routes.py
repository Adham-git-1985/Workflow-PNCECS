# archive/routes.py

import os
import uuid
from datetime import datetime

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
}


# =========================
# Helpers
# =========================
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



# =========================
# Sign PDF (flag only)
# =========================
@archive_bp.route("/sign/<int:file_id>", methods=["POST"], endpoint="sign_pdf")
@login_required
@roles_required("ADMIN")
def sign_pdf(file_id):
    file = ArchivedFile.query.get_or_404(file_id)

    if file.is_deleted:
        abort(404)

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

        file = request.files.get("file")
        description = request.form.get("description")

        if not file or not file.filename:
            flash("No file selected", "danger")
            return redirect(request.url)

        if not allowed_file(file.filename):
            flash("File type not allowed", "danger")
            return redirect(request.url)

        os.makedirs(BASE_STORAGE, exist_ok=True)

        import os as _os
        original_name = (file.filename or "").strip()
        original_name = _os.path.basename(original_name).replace("\x00", "")

        if not original_name:
            flash("اسم الملف غير صالح", "danger")
            return redirect(request.url)

        if "." not in original_name:
            flash("الملف بدون امتداد (مثال: .pdf / .jpg).", "danger")
            return redirect(request.url)

        ext = original_name.rsplit(".", 1)[1].lower().strip()
        if not ext:
            flash("امتداد الملف غير صالح.", "danger")
            return redirect(request.url)

        stored_name = f"{uuid.uuid4().hex}.{ext}"
        file_path = os.path.join(BASE_STORAGE, stored_name)

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
                visibility="owner"
            )

            db.session.add(archived)
            db.session.flush()

            send_to_workflow = request.form.get("send_to_workflow") == "1"
            template_id = (request.form.get("template_id") or "").strip()

            if send_to_workflow:
                if not template_id.isdigit():
                    db.session.rollback()
                    try:
                        if os.path.exists(file_path):
                            os.remove(file_path)
                    except Exception:
                        pass
                    flash("يرجى اختيار مسار (Template) لبدء مسار العمل.", "danger")
                    return redirect(request.url)

                req = WorkflowRequest(
                    requester_id=current_user.id,
                    status="DRAFT",
                    title=request.form.get("request_title", "طلب مرفق من الأرشيف"),
                    description=request.form.get("request_description", "")
                )
                db.session.add(req)
                db.session.flush()

                db.session.add(RequestAttachment(
                    request_id=req.id,
                    archived_file_id=archived.id
                ))

                template = WorkflowTemplate.query.get_or_404(int(template_id))
                start_workflow_for_request(
                    req,
                    template,
                    created_by_user_id=current_user.id,
                    auto_commit=False
                )

                emit_event(
                    actor_id=current_user.id,
                    action="ARCHIVE_UPLOADED",
                    message=f"تم رفع ملف أرشيف: {archived.original_name}",
                    target_type="ARCHIVE_FILE",
                    target_id=archived.id,
                    notify_role="ADMIN",
                    auto_commit=False
                )

                # ✅ مهم: تثبيت emit_event داخل مسار الـ workflow
                db.session.commit()

                flash("تم رفع الملف وبدء مسار العمل بنجاح", "success")
                return redirect(url_for("workflow.view_request", request_id=req.id))

            # Archive only
            emit_event(
                actor_id=current_user.id,
                action="ARCHIVE_UPLOADED",
                message=f"تم رفع ملف أرشيف: {archived.original_name}",
                target_type="ARCHIVE_FILE",
                target_id=archived.id,
                notify_role="ADMIN",
                auto_commit=False
            )

            db.session.commit()


            flash("File uploaded successfully", "success")
            return redirect(url_for("archive.my_files"))

        except Exception as e:
            db.session.rollback()
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
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
@roles_required("ADMIN")
def delete_file(file_id):
    file = ArchivedFile.query.get_or_404(file_id)

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
@roles_required("ADMIN")
def recycle_bin():
    files = ArchivedFile.query.filter(
        ArchivedFile.is_deleted.is_(True)
    ).order_by(
        ArchivedFile.deleted_at.desc()
    ).all()

    return render_template("archive/recycle_bin.html", files=files)


@archive_bp.route("/restore/<int:file_id>")
@login_required
@roles_required("ADMIN")
def restore_file(file_id):
    file = ArchivedFile.query.get_or_404(file_id)

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
@roles_required("ADMIN")
def archive_audit_log():
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
