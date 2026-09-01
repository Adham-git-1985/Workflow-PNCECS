from datetime import datetime, timedelta
import mimetypes
import re
from pathlib import Path
import uuid

from sqlalchemy import or_

from flask import current_app, render_template, request, redirect, url_for, flash, abort, send_from_directory
from flask_login import login_required, current_user

from extensions import db
from utils.events import emit_event
from utils.file_uploads import clean_original_filename, is_allowed_attachment, random_storage_name
from . import messages_bp
from models import (
    User, Department, Directorate,
    Message, MessageAttachment, MessageRecipient, AuditLog
)


MESSAGE_ATTACHMENT_MAX_FILES = 10
MESSAGE_ATTACHMENT_MAX_FILE_BYTES = 25 * 1024 * 1024
MESSAGE_ATTACHMENT_MAX_TOTAL_BYTES = 50 * 1024 * 1024
MESSAGE_ATTACHMENT_MAX_REQUEST_BYTES = MESSAGE_ATTACHMENT_MAX_TOTAL_BYTES + (2 * 1024 * 1024)


def _message_attachment_dir(message_id: int) -> Path:
    directory = Path(current_app.instance_path) / "uploads" / "messages" / str(int(message_id))
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _message_uploads() -> list:
    return [
        upload
        for upload in (request.files.getlist("attachments") or [])
        if upload and getattr(upload, "filename", "")
    ]


def _save_message_attachments(message: Message, uploads) -> tuple[int, list[Path]]:
    """Persist message documents under the instance folder with bounded size."""
    candidates = []
    for upload in uploads or []:
        original_name = clean_original_filename(getattr(upload, "filename", None))
        if not original_name or not is_allowed_attachment(original_name):
            raise ValueError("اسم أحد المرفقات غير صالح.")
        candidates.append((upload, original_name))

    if len(candidates) > MESSAGE_ATTACHMENT_MAX_FILES:
        raise ValueError(f"يمكن إرفاق {MESSAGE_ATTACHMENT_MAX_FILES} ملفات كحد أقصى.")

    saved_paths: list[Path] = []
    total_size = 0
    try:
        for upload, original_name in candidates:
            stored_name = random_storage_name(uuid.uuid4().hex, original_name)
            saved_path = _message_attachment_dir(message.id) / stored_name
            upload.save(str(saved_path))
            saved_paths.append(saved_path)

            file_size = saved_path.stat().st_size
            if file_size > MESSAGE_ATTACHMENT_MAX_FILE_BYTES:
                raise ValueError(f"حجم الملف «{original_name}» يتجاوز الحد المسموح (25 م.ب).")
            total_size += file_size
            if total_size > MESSAGE_ATTACHMENT_MAX_TOTAL_BYTES:
                raise ValueError("إجمالي حجم مرفقات الرسالة يتجاوز الحد المسموح (50 م.ب).")

            mime_type = (getattr(upload, "mimetype", None) or "").strip()
            if not mime_type:
                mime_type = mimetypes.guess_type(original_name)[0] or "application/octet-stream"
            db.session.add(MessageAttachment(
                message=message,
                original_name=original_name[:255],
                stored_name=stored_name,
                mime_type=mime_type[:120],
                file_size=file_size,
                uploaded_by_id=int(current_user.id),
                uploaded_at=datetime.utcnow(),
            ))
    except Exception:
        for saved_path in saved_paths:
            try:
                saved_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise

    return len(candidates), saved_paths


def _remove_message_attachment_files(paths) -> None:
    for saved_path in paths or []:
        try:
            Path(saved_path).unlink(missing_ok=True)
        except OSError:
            pass


def _can_access_message(message: Message, user: User) -> bool:
    if int(getattr(message, "sender_id", 0) or 0) == int(getattr(user, "id", 0) or 0):
        return not bool(getattr(message, "sender_deleted", False))
    return (
        MessageRecipient.query
        .filter(
            MessageRecipient.message_id == message.id,
            MessageRecipient.recipient_user_id == user.id,
            MessageRecipient.is_deleted.is_(False),
        )
        .first()
        is not None
    )


def _audit_message(action, msg, note_extra=None, recipients=None):
    """Create MESSAGE_* audit log entry (visible only to SUPER_ADMIN via audit filters)."""
    try:
        subj = (msg.subject or "").strip() or "(بدون موضوع)"
        body = (msg.body or "").strip()
        rtxt = ""
        if recipients:
            rtxt = f"Recipients={recipients} | "
        extra = f" | {note_extra}" if note_extra else ""
        note = (
            f"Message#{msg.id} | From={msg.sender_id} | To={msg.target_kind}:{msg.target_id} | "
            f"{rtxt}Subject={subj}\n\nBODY:\n{body}{extra}"
        )
        db.session.add(
            AuditLog(
                action=action,
                user_id=current_user.id,
                target_type="Message",
                target_id=msg.id,
                note=note
            )
        )
    except Exception:
        # Never block normal flow if auditing fails.
        pass


def _find_recent_duplicate_message(sender_id, target_kind, target_id, subject, body):
    duplicate_window = datetime.utcnow() - timedelta(seconds=15)
    return (
        Message.query
        .filter(
            Message.sender_id == sender_id,
            Message.sender_deleted.is_(False),
            Message.target_kind == target_kind,
            Message.target_id == target_id,
            Message.subject == subject,
            Message.body == body,
            Message.created_at >= duplicate_window,
        )
        .order_by(Message.created_at.desc())
        .first()
    )


@messages_bp.route("/inbox")
@login_required
def inbox():
    page = request.args.get("page", 1, type=int)
    search = (request.args.get("q") or "").strip()

    q = (
        db.session.query(MessageRecipient)
        .join(MessageRecipient.message)
        .filter(
            MessageRecipient.recipient_user_id == current_user.id,
            MessageRecipient.is_deleted.is_(False),
            Message.is_system_generated.is_(False),
        )
    )

    if search:
        like = f"%{search}%"
        q = q.filter(
            or_(
                Message.subject.ilike(like),
                Message.body.ilike(like),
                Message.sender.has(User.email.ilike(like))
            )
        )

    q = q.order_by(Message.created_at.desc())

    pagination = q.paginate(page=page, per_page=20, error_out=False)
    unread_count = (
        MessageRecipient.query
        .join(MessageRecipient.message)
        .filter(
            MessageRecipient.recipient_user_id == current_user.id,
            MessageRecipient.is_deleted.is_(False),
            MessageRecipient.is_read.is_(False),
            Message.is_system_generated.is_(False),
        )
        .count()
    )

    return render_template(
        "messages/inbox.html",
        items=pagination.items,
        pagination=pagination,
        q=search,
        unread_count=unread_count,
    )


@messages_bp.route("/inbox/mark-all-read", methods=["POST"])
@login_required
def mark_all_messages_read():
    """Mark every non-deleted correspondence message for the current recipient as read."""
    try:
        from datetime import datetime

        updated = (
            MessageRecipient.query
            .filter(
                MessageRecipient.recipient_user_id == current_user.id,
                MessageRecipient.is_deleted.is_(False),
                MessageRecipient.is_read.is_(False),
                MessageRecipient.message_id.in_(
                    db.session.query(Message.id).filter(Message.is_system_generated.is_(False))
                ),
            )
            .update(
                {"is_read": True, "read_at": datetime.utcnow()},
                synchronize_session=False,
            )
        )
        db.session.commit()
        if updated:
            flash("تم تعليم جميع المراسلات كمقروءة.", "success")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Failed to mark inbox messages as read for user_id=%s", current_user.id)
        flash("تعذر تحديث حالة المراسلات.", "danger")

    return redirect(url_for("messages.inbox", q=(request.form.get("q") or "").strip()))


@messages_bp.route("/sent")
@login_required
def sent():
    page = request.args.get("page", 1, type=int)
    search = (request.args.get("q") or "").strip()

    q = (
        Message.query
        .filter(
            Message.sender_id == current_user.id,
            Message.sender_deleted.is_(False),
            Message.is_system_generated.is_(False),
        )
    )

    if search:
        like = f"%{search}%"
        q = q.filter(
            or_(
                Message.subject.ilike(like),
                Message.body.ilike(like),
                Message.recipients.any(
                    MessageRecipient.recipient.has(
                        or_(
                            User.email.ilike(like),
                            User.name.ilike(like)
                        )
                    )
                )
            )
        )

    q = q.order_by(Message.created_at.desc())

    pagination = q.paginate(page=page, per_page=20, error_out=False)
    return render_template(
        "messages/sent.html",
        messages=pagination.items,
        pagination=pagination,
        q=search
    )


@messages_bp.route("/compose", methods=["GET", "POST"])
@login_required
def compose():
    users = User.query.order_by(User.email.asc()).all()
    departments = Department.query.order_by(Department.name_ar.asc()).all()
    directorates = Directorate.query.order_by(Directorate.name_ar.asc()).all()

    if request.method == "POST":
        if (
            request.content_length is not None
            and request.content_length > MESSAGE_ATTACHMENT_MAX_REQUEST_BYTES
        ):
            flash("حجم طلب الرسالة يتجاوز الحد المسموح للمرفقات (50 م.ب إجمالاً).", "danger")
            return redirect(url_for("messages.compose"))

        target_kind = (request.form.get("target_kind") or "").strip().upper()
        target_id = request.form.get("target_id")
        subject = (request.form.get("subject") or "").strip()
        body = (request.form.get("body") or "").strip()
        uploads = _message_uploads()

        if target_kind not in {"USER", "DEPARTMENT", "DIRECTORATE"}:
            flash("يرجى اختيار جهة صحيحة", "danger")
            return redirect(url_for("messages.compose"))

        try:
            target_id_int = int(target_id)
        except Exception:
            flash("يرجى اختيار جهة صحيحة", "danger")
            return redirect(url_for("messages.compose"))

        if not body and not uploads:
            flash("يرجى كتابة نص الرسالة أو إرفاق مستند.", "danger")
            return redirect(url_for("messages.compose"))

        # Resolve recipients
        recipient_ids = []

        if target_kind == "USER":
            u = User.query.get(target_id_int)
            if not u:
                flash("المستخدم غير موجود", "danger")
                return redirect(url_for("messages.compose"))
            recipient_ids = [u.id]

        elif target_kind == "DEPARTMENT":
            dept = Department.query.get(target_id_int)
            if not dept:
                flash("الدائرة غير موجودة", "danger")
                return redirect(url_for("messages.compose"))
            recipient_ids = [
                uid for (uid,) in (
                    db.session.query(User.id)
                    .filter(User.department_id == dept.id)
                    .all()
                )
            ]

        elif target_kind == "DIRECTORATE":
            dir_ = Directorate.query.get(target_id_int)
            if not dir_:
                flash("الإدارة غير موجودة", "danger")
                return redirect(url_for("messages.compose"))
            dept_ids = [
                did for (did,) in (
                    db.session.query(Department.id)
                    .filter(Department.directorate_id == dir_.id)
                    .all()
                )
            ]
            recipient_ids = []
            if dept_ids:
                recipient_ids = [
                    uid for (uid,) in (
                        db.session.query(User.id)
                        .filter(User.department_id.in_(dept_ids))
                        .all()
                    )
                ]

        # remove duplicates + exclude sender
        recipient_ids = sorted({rid for rid in recipient_ids if rid and rid != current_user.id})

        if not recipient_ids:
            flash("لا يوجد مستخدمون ضمن الجهة المختارة", "warning")
            return redirect(url_for("messages.compose"))

        # File-only messages can legitimately share the same subject/body in
        # quick succession. The compose form already prevents double submits,
        # so keep the legacy text-only duplicate guard out of their way.
        duplicate = None if uploads else _find_recent_duplicate_message(
            sender_id=current_user.id,
            target_kind=target_kind,
            target_id=target_id_int,
            subject=subject,
            body=body,
        )
        if duplicate:
            flash("تم تجاهل محاولة إرسال مكررة لنفس الرسالة.", "warning")
            return redirect(url_for("messages.sent"))

        msg = Message(
            sender_id=current_user.id,
            subject=subject,
            body=body,
            target_kind=target_kind,
            target_id=target_id_int,
            created_at=datetime.utcnow(),
            reply_to_id=None
        )
        db.session.add(msg)
        db.session.flush()

        saved_paths = []
        try:
            attachment_count, saved_paths = _save_message_attachments(msg, uploads)
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return redirect(url_for("messages.compose"))
        except Exception:
            db.session.rollback()
            current_app.logger.exception("Failed to save internal message attachments")
            flash("تعذر حفظ مرفقات الرسالة.", "danger")
            return redirect(url_for("messages.compose"))

        # Recipients rows
        rec_rows = [
            MessageRecipient(
                message_id=msg.id,
                recipient_user_id=rid,
                is_read=False,
                read_at=None,
                is_deleted=False,
                deleted_at=None
            )
            for rid in recipient_ids
        ]
        db.session.add_all(rec_rows)

        # Audit (MESSAGE_* hidden for non-SUPER_ADMIN)
        _audit_message(
            action="MESSAGE_SENT",
            msg=msg,
            note_extra=f"attachments={attachment_count}",
            recipients=",".join(map(str, recipient_ids))
        )

        # Notifications to recipients (bell + SSE)
        sender_label = current_user.email
        subj = subject or "(بدون موضوع)"
        for rid in recipient_ids:
            emit_event(
                actor_id=current_user.id,
                action="MESSAGE_SENT",
                message=f"رسالة جديدة من {sender_label}: {subj}",
                target_type="Message",
                target_id=msg.id,
                notify_user_id=rid,
                level="INFO",
                auto_commit=False
            )

        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            _remove_message_attachment_files(saved_paths)
            current_app.logger.exception("Failed to send internal message")
            flash("تعذر إرسال الرسالة.", "danger")
            return redirect(url_for("messages.compose"))
        flash(
            "تم إرسال الرسالة"
            + (f" مع {attachment_count} مرفق/مرفقات." if attachment_count else "."),
            "success",
        )
        return redirect(url_for("messages.sent"))

    return render_template(
        "messages/compose.html",
        users=users,
        departments=departments,
        directorates=directorates
    )


@messages_bp.route("/reply/<int:message_id>", methods=["GET", "POST"])
@login_required
def reply(message_id):
    original = Message.query.get_or_404(message_id)

    rec = (
        MessageRecipient.query
        .filter(
            MessageRecipient.message_id == message_id,
            MessageRecipient.recipient_user_id == current_user.id,
            MessageRecipient.is_deleted.is_(False)
        )
        .first()
    )

    # Reply is available only for recipients
    if not rec:
        flash("لا تملك صلاحية للرد على هذه الرسالة", "danger")
        return redirect(url_for("messages.inbox"))

    if request.method == "POST":
        if (
            request.content_length is not None
            and request.content_length > MESSAGE_ATTACHMENT_MAX_REQUEST_BYTES
        ):
            flash("حجم طلب الرد يتجاوز الحد المسموح للمرفقات (50 م.ب إجمالاً).", "danger")
            return redirect(url_for("messages.reply", message_id=message_id))

        subject = (request.form.get("subject") or "").strip()
        body = (request.form.get("body") or "").strip()
        uploads = _message_uploads()
        if not body and not uploads:
            flash("يرجى كتابة نص الرد أو إرفاق مستند.", "danger")
            return redirect(url_for("messages.reply", message_id=message_id))

        # send to original sender
        if original.sender_id == current_user.id:
            flash("لا يمكنك الرد على نفسك", "warning")
            return redirect(url_for("messages.inbox"))

        reply_msg = Message(
            sender_id=current_user.id,
            subject=subject or f"RE: {(original.subject or '').strip() or '(بدون موضوع)'}",
            body=body,
            target_kind="USER",
            target_id=int(original.sender_id),
            created_at=datetime.utcnow(),
            reply_to_id=original.id
        )
        db.session.add(reply_msg)
        db.session.flush()

        saved_paths = []
        try:
            attachment_count, saved_paths = _save_message_attachments(reply_msg, uploads)
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return redirect(url_for("messages.reply", message_id=message_id))
        except Exception:
            db.session.rollback()
            current_app.logger.exception("Failed to save internal message reply attachments")
            flash("تعذر حفظ مرفقات الرد.", "danger")
            return redirect(url_for("messages.reply", message_id=message_id))

        db.session.add(
            MessageRecipient(
                message_id=reply_msg.id,
                recipient_user_id=int(original.sender_id),
                is_read=False,
                read_at=None,
                is_deleted=False,
                deleted_at=None
            )
        )

        _audit_message(
            action="MESSAGE_REPLY_SENT",
            msg=reply_msg,
            note_extra=f"reply_to={original.id} | attachments={attachment_count}",
            recipients=str(original.sender_id)
        )

        emit_event(
            actor_id=current_user.id,
            action="MESSAGE_REPLY_SENT",
            message=f"رد جديد من {current_user.email}: {reply_msg.subject}",
            target_type="Message",
            target_id=reply_msg.id,
            notify_user_id=int(original.sender_id),
            level="INFO",
            auto_commit=False
        )

        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            _remove_message_attachment_files(saved_paths)
            current_app.logger.exception("Failed to send internal message reply")
            flash("تعذر إرسال الرد.", "danger")
            return redirect(url_for("messages.reply", message_id=message_id))
        flash(
            "تم إرسال الرد"
            + (f" مع {attachment_count} مرفق/مرفقات." if attachment_count else "."),
            "success",
        )
        return redirect(url_for("messages.sent"))

    default_subject = f"RE: {(original.subject or '').strip() or '(بدون موضوع)'}"
    return render_template(
        "messages/reply.html",
        original=original,
        default_subject=default_subject
    )


@messages_bp.route("/attachment/<int:attachment_id>/download")
@login_required
def download_attachment(attachment_id: int):
    attachment = MessageAttachment.query.get_or_404(attachment_id)
    message = Message.query.get_or_404(attachment.message_id)

    if not _can_access_message(message, current_user):
        abort(403)

    stored_name = Path(attachment.stored_name or "").name
    if not stored_name or stored_name != (attachment.stored_name or ""):
        abort(404)
    directory = Path(current_app.instance_path) / "uploads" / "messages" / str(int(message.id))
    if not (directory / stored_name).is_file():
        abort(404)

    return send_from_directory(
        str(directory),
        stored_name,
        mimetype=attachment.mime_type or None,
        as_attachment=True,
        download_name=attachment.original_name,
    )


@messages_bp.route("/delete/<int:message_id>", methods=["POST"])
@login_required
def delete_message(message_id):
    msg = Message.query.get_or_404(message_id)

    # recipient delete (soft)
    rec = (
        MessageRecipient.query
        .filter(
            MessageRecipient.message_id == message_id,
            MessageRecipient.recipient_user_id == current_user.id
        )
        .first()
    )

    if rec and not rec.is_deleted:
        rec.is_deleted = True
        rec.deleted_at = datetime.utcnow()
        _audit_message(
            action="MESSAGE_DELETED",
            msg=msg,
            note_extra=f"deleted_by_recipient={current_user.id}"
        )
        db.session.commit()
        flash("تم حذف الرسالة من صندوق الوارد", "success")
        return redirect(url_for("messages.inbox"))

    # sender delete (soft)
    if msg.sender_id == current_user.id and not msg.sender_deleted:
        msg.sender_deleted = True
        msg.sender_deleted_at = datetime.utcnow()
        _audit_message(
            action="MESSAGE_DELETED",
            msg=msg,
            note_extra=f"deleted_by_sender={current_user.id}"
        )
        db.session.commit()
        flash("تم حذف الرسالة من المرسلة", "success")
        return redirect(url_for("messages.sent"))

    abort(403)


@messages_bp.route("/view/<int:message_id>")
@login_required
def view_message(message_id):
    # Allow both:
    # - recipient opens the message (mark as read)
    # - sender opens a sent message (read-only view)
    rec = (
        MessageRecipient.query
        .filter(
            MessageRecipient.message_id == message_id,
            MessageRecipient.recipient_user_id == current_user.id,
            MessageRecipient.is_deleted.is_(False)
        )
        .first()
    )

    msg = Message.query.get_or_404(message_id)

    # Sender can't view a message they have deleted from "sent"
    if msg.sender_id == current_user.id and msg.sender_deleted:
        flash("هذه الرسالة محذوفة من المرسلة", "warning")
        return redirect(url_for("messages.sent"))

    if not rec and msg.sender_id != current_user.id:
        flash("لا تملك صلاحية لعرض هذه الرسالة", "danger")
        return redirect(url_for("messages.inbox"))

    # mark read (only for recipients)
    if rec and not rec.is_read:
        rec.is_read = True
        rec.read_at = datetime.utcnow()
        db.session.commit()

    # Optional: detect known internal links and show quick action buttons.
    payslip_url = None
    workflow_url = None
    meeting_url = None
    movement_url = None
    supply_request_url = None
    try:
        if rec and msg and msg.body:
            m = re.search(r"/portal/hr/me/payslips/(\d+)/view", msg.body)
            if m:
                att_id = int(m.group(1))
                payslip_url = url_for("portal.hr_my_payslip_view", att_id=att_id)
    except Exception:
        payslip_url = None

    try:
        if msg and msg.body:
            m = re.search(r"/workflow/request/(\d+)", msg.body)
            if m:
                workflow_url = url_for("workflow.view_request", request_id=int(m.group(1)))
    except Exception:
        workflow_url = None

    try:
        if msg and msg.body:
            m = re.search(r"/portal/meetings/(\d+)", msg.body)
            if m:
                meeting_url = url_for("portal.meeting_view", meeting_id=int(m.group(1)))
    except Exception:
        meeting_url = None

    try:
        if msg and msg.body:
            m = re.search(r"/portal/transport/permits/(\d+)", msg.body)
            if m:
                movement_url = url_for("portal.transport_permit_view", permit_id=int(m.group(1)))
    except Exception:
        movement_url = None

    try:
        if msg and msg.body:
            m = re.search(r"/portal/inventory/employee-requests/(\d+)", msg.body)
            if m:
                supply_request_url = url_for("portal.inventory_employee_request_view", request_id=int(m.group(1)))
    except Exception:
        supply_request_url = None

    return render_template(
        "messages/view.html",
        rec=rec,
        msg=msg,
        payslip_url=payslip_url,
        workflow_url=workflow_url,
        meeting_url=meeting_url,
        movement_url=movement_url,
        supply_request_url=supply_request_url,
    )
