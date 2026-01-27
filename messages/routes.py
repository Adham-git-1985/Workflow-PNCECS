from datetime import datetime

from sqlalchemy import or_

from flask import render_template, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user

from extensions import db
from utils.events import emit_event
from . import messages_bp
from models import (
    User, Department, Directorate,
    Message, MessageRecipient, AuditLog
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
            MessageRecipient.is_deleted.is_(False)
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

    return render_template(
        "messages/inbox.html",
        items=pagination.items,
        pagination=pagination,
        q=search
    )


@messages_bp.route("/sent")
@login_required
def sent():
    page = request.args.get("page", 1, type=int)

    q = (
        Message.query
        .filter(
            Message.sender_id == current_user.id,
            Message.sender_deleted.is_(False)
        )
        .order_by(Message.created_at.desc())
    )

    pagination = q.paginate(page=page, per_page=20, error_out=False)
    return render_template(
        "messages/sent.html",
        messages=pagination.items,
        pagination=pagination
    )


@messages_bp.route("/compose", methods=["GET", "POST"])
@login_required
def compose():
    users = User.query.order_by(User.email.asc()).all()
    departments = Department.query.order_by(Department.name_ar.asc()).all()
    directorates = Directorate.query.order_by(Directorate.name_ar.asc()).all()

    if request.method == "POST":
        target_kind = (request.form.get("target_kind") or "").strip().upper()
        target_id = request.form.get("target_id")
        subject = (request.form.get("subject") or "").strip()
        body = (request.form.get("body") or "").strip()

        if target_kind not in {"USER", "DEPARTMENT", "DIRECTORATE"}:
            flash("يرجى اختيار جهة صحيحة", "danger")
            return redirect(url_for("messages.compose"))

        try:
            target_id_int = int(target_id)
        except Exception:
            flash("يرجى اختيار جهة صحيحة", "danger")
            return redirect(url_for("messages.compose"))

        if not body:
            flash("يرجى كتابة نص الرسالة", "danger")
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

        db.session.commit()
        flash("تم إرسال الرسالة", "success")
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
        subject = (request.form.get("subject") or "").strip()
        body = (request.form.get("body") or "").strip()
        if not body:
            flash("يرجى كتابة نص الرد", "danger")
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
            note_extra=f"reply_to={original.id}",
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

        db.session.commit()
        flash("تم إرسال الرد", "success")
        return redirect(url_for("messages.sent"))

    default_subject = f"RE: {(original.subject or '').strip() or '(بدون موضوع)'}"
    return render_template(
        "messages/reply.html",
        original=original,
        default_subject=default_subject
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

    return render_template("messages/view.html", rec=rec, msg=msg)
