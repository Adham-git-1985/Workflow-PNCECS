from datetime import datetime
from functools import wraps
from pathlib import Path
import mimetypes
import uuid

from flask import abort, current_app, flash, jsonify, redirect, render_template, request, send_from_directory, url_for
from flask_login import current_user, login_required
from sqlalchemy import and_, func, or_

from extensions import db
from models import AuditLog, ChatAttachment, ChatConversation, ChatMessage, ChatParticipant, User, WorkflowInstance, WorkflowStepTask, WorkflowRequest
from utils.events import emit_event
from utils.file_uploads import clean_original_filename, is_allowed_attachment, is_safe_inline_mimetype, random_storage_name
from . import chats_bp

CHAT_ACCESS = "CHAT_ACCESS"
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


def chat_access_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.has_perm(CHAT_ACCESS):
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def _participant(conversation_id):
    return ChatParticipant.query.filter_by(conversation_id=conversation_id, user_id=current_user.id).first()


def _direct_conversation(other_user_id):
    mine = db.session.query(ChatParticipant.conversation_id).filter_by(user_id=current_user.id)
    return (ChatConversation.query.join(ChatParticipant)
            .filter(ChatConversation.kind == "DIRECT", ChatConversation.id.in_(mine))
            .filter(ChatConversation.id.in_(db.session.query(ChatParticipant.conversation_id).filter_by(user_id=other_user_id)))
            .first())


def _unread_count(user_id):
    rows = ChatParticipant.query.filter_by(user_id=user_id).all()
    total = 0
    for row in rows:
        query = ChatMessage.query.filter(ChatMessage.conversation_id == row.conversation_id, ChatMessage.sender_id != user_id)
        if row.last_read_at:
            query = query.filter(ChatMessage.created_at > row.last_read_at)
        if query.first():
            total += 1
    return total


@chats_bp.route("/")
@login_required
@chat_access_required
def inbox():
    search = (request.args.get("q") or "").strip()
    conversations = (ChatConversation.query.join(ChatParticipant)
        .filter(ChatParticipant.user_id == current_user.id)
        .order_by(ChatConversation.updated_at.desc()).all())
    conversations.sort(key=lambda c: (not bool(next((p.is_pinned for p in c.participants if p.user_id == current_user.id), False)), c.updated_at))
    if search:
        needle = f"%{search}%"
        matching_ids = db.session.query(ChatMessage.conversation_id).filter(ChatMessage.body.ilike(needle))
        conversations = [c for c in conversations if c.id in set(cid for (cid,) in matching_ids.all()) or search.lower() in (c.title or "").lower()]
    users = User.query.order_by(User.name.asc(), User.email.asc()).all()
    eligible_users = [u for u in users if u.id != current_user.id and u.has_perm(CHAT_ACCESS)]
    return render_template("chats/inbox.html", conversations=conversations, eligible_users=eligible_users, unread_count=_unread_count(current_user.id), q=search)


@chats_bp.route("/unread-count")
@login_required
@chat_access_required
def unread_count():
    return jsonify({"count": _unread_count(current_user.id)})


@chats_bp.route("/<int:conversation_id>/mute", methods=["POST"])
@login_required
@chat_access_required
def toggle_mute(conversation_id):
    participant = _participant(conversation_id)
    if not participant:
        abort(403)
    participant.is_muted = not participant.is_muted
    db.session.commit()
    return redirect(url_for("chats.conversation", conversation_id=conversation_id))


@chats_bp.route("/<int:conversation_id>/pin", methods=["POST"])
@login_required
@chat_access_required
def toggle_pin(conversation_id):
    participant = _participant(conversation_id)
    if not participant:
        abort(403)
    participant.is_pinned = not participant.is_pinned
    db.session.commit()
    return redirect(url_for("chats.conversation", conversation_id=conversation_id))


@chats_bp.route("/<int:conversation_id>/leave", methods=["POST"])
@login_required
@chat_access_required
def leave_group(conversation_id):
    conversation = ChatConversation.query.get_or_404(conversation_id)
    participant = _participant(conversation_id)
    if not participant or conversation.kind != "GROUP":
        abort(403)
    db.session.delete(participant)
    db.session.add(AuditLog(user_id=current_user.id, action="CHAT_MEMBER_LEFT", target_type="ChatConversation", target_id=conversation_id, note=f"User {current_user.id} left group"))
    db.session.commit()
    return redirect(url_for("chats.inbox"))


@chats_bp.route("/<int:conversation_id>/members", methods=["POST"])
@login_required
@chat_access_required
def update_members(conversation_id):
    conversation = ChatConversation.query.get_or_404(conversation_id)
    if conversation.kind != "GROUP" or conversation.created_by_id != current_user.id:
        abort(403)
    old_ids = {p.user_id for p in conversation.participants}
    ids = {int(v) for v in request.form.getlist("user_ids") if v.isdigit()}
    title = (request.form.get("title") or "").strip()[:200]
    ids.add(current_user.id)
    users = User.query.filter(User.id.in_(ids)).all()
    if any(not u.has_perm(CHAT_ACCESS) for u in users):
        abort(403)
    ChatParticipant.query.filter_by(conversation_id=conversation_id).delete(synchronize_session=False)
    db.session.add_all([ChatParticipant(conversation_id=conversation_id, user_id=u.id) for u in users])
    if title:
        conversation.title = title
    new_ids = {u.id for u in users}
    for uid in sorted(new_ids - old_ids):
        emit_event(actor_id=current_user.id, action="CHAT_MEMBER_ADDED", message="تمت إضافتك إلى مجموعة محادثة", target_type="ChatConversation", target_id=conversation_id, notify_user_id=uid, level="INFO", auto_commit=False)
    for uid in sorted(old_ids - new_ids):
        emit_event(actor_id=current_user.id, action="CHAT_MEMBER_REMOVED", message="تمت إزالتك من مجموعة محادثة", target_type="ChatConversation", target_id=conversation_id, notify_user_id=uid, level="INFO", auto_commit=False)
    db.session.add(AuditLog(user_id=current_user.id, action="CHAT_MEMBERS_UPDATED", target_type="ChatConversation", target_id=conversation_id, note=f"members={sorted(new_ids)}"))
    db.session.commit()
    return redirect(url_for("chats.conversation", conversation_id=conversation_id))


@chats_bp.route("/<int:conversation_id>/delete", methods=["POST"])
@login_required
def delete_group(conversation_id):
    """Administrative deletion without granting the administrator message access."""
    if not (current_user.has_role("ADMIN") or current_user.has_role("SUPER_ADMIN") or current_user.has_role("SUPERADMIN")):
        abort(403)
    conversation = ChatConversation.query.get_or_404(conversation_id)
    if conversation.kind != "GROUP":
        abort(400)
    db.session.delete(conversation)
    db.session.add(AuditLog(user_id=current_user.id, action="CHAT_GROUP_DELETED", target_type="ChatConversation", target_id=conversation_id, note="Administrative group deletion"))
    db.session.commit()
    flash("تم حذف المجموعة إداريًا.", "success")
    return redirect(url_for("chats.inbox"))


@chats_bp.route("/group", methods=["POST"])
@login_required
@chat_access_required
def start_group():
    title = (request.form.get("title") or "").strip()[:200]
    try:
        ids = {int(v) for v in request.form.getlist("user_ids")}
    except ValueError:
        ids = set()
    ids.add(current_user.id)
    users = User.query.filter(User.id.in_(ids)).all()
    if len(users) < 3 or any(not user.has_perm(CHAT_ACCESS) for user in users):
        flash("اختر مستخدمين مخوّلين اثنين على الأقل للمجموعة.", "warning")
        return redirect(url_for("chats.inbox"))
    conversation = ChatConversation(title=title or "مجموعة جديدة", kind="GROUP", created_by_id=current_user.id)
    db.session.add(conversation); db.session.flush()
    db.session.add_all([ChatParticipant(conversation_id=conversation.id, user_id=user.id) for user in users])
    db.session.commit()
    return redirect(url_for("chats.conversation", conversation_id=conversation.id))


@chats_bp.route("/direct/<int:user_id>", methods=["POST"])
@login_required
@chat_access_required
def start_direct(user_id):
    other = User.query.get_or_404(user_id)
    if other.id == current_user.id or not other.has_perm(CHAT_ACCESS):
        abort(403)
    conversation = _direct_conversation(other.id)
    if not conversation:
        conversation = ChatConversation(kind="DIRECT", created_by_id=current_user.id)
        db.session.add(conversation)
        db.session.flush()
        db.session.add_all([
            ChatParticipant(conversation_id=conversation.id, user_id=current_user.id),
            ChatParticipant(conversation_id=conversation.id, user_id=other.id),
        ])
        db.session.commit()
    return redirect(url_for("chats.conversation", conversation_id=conversation.id))


@chats_bp.route("/workflow/<int:request_id>/open", methods=["POST"])
@login_required
@chat_access_required
def open_workflow_chat(request_id):
    """Create/open the protected coordination thread for one workflow request."""
    req = WorkflowRequest.query.get_or_404(request_id)
    from workflow.routes import _user_can_view_request
    if not _user_can_view_request(current_user, req):
        abort(403)
    conversation = ChatConversation.query.filter_by(workflow_request_id=req.id, kind="WORKFLOW").first()
    if not conversation:
        participant_ids = {current_user.id, req.requester_id}
        instance = WorkflowInstance.query.filter_by(request_id=req.id).first()
        if instance:
            participant_ids.update(uid for (uid,) in db.session.query(WorkflowStepTask.assignee_user_id).filter_by(instance_id=instance.id).all() if uid)
        allowed_ids = {u.id for u in User.query.filter(User.id.in_(participant_ids)).all() if u.has_perm(CHAT_ACCESS)}
        allowed_ids.add(current_user.id)
        conversation = ChatConversation(title=f"محادثة الطلب #{req.id}", kind="WORKFLOW", workflow_request_id=req.id, created_by_id=current_user.id)
        db.session.add(conversation)
        db.session.flush()
        db.session.add_all([ChatParticipant(conversation_id=conversation.id, user_id=uid) for uid in allowed_ids])
        db.session.commit()
    if not ChatParticipant.query.filter_by(conversation_id=conversation.id, user_id=current_user.id).first():
        abort(403)
    return redirect(url_for("chats.conversation", conversation_id=conversation.id))


@chats_bp.route("/<int:conversation_id>", methods=["GET", "POST"])
@login_required
@chat_access_required
def conversation(conversation_id):
    conversation = ChatConversation.query.get_or_404(conversation_id)
    membership = _participant(conversation_id)
    if not membership:
        abort(403)

    if request.method == "POST":
        body = (request.form.get("body") or "").strip()
        uploads = [u for u in request.files.getlist("attachments") if u and u.filename]
        if not body and not uploads:
            flash("اكتب رسالة أولاً.", "warning")
            return redirect(url_for("chats.conversation", conversation_id=conversation_id))
        msg = ChatMessage(conversation_id=conversation_id, sender_id=current_user.id, body=body)
        conversation.updated_at = datetime.utcnow()
        db.session.add(msg); db.session.flush()
        upload_dir = Path(current_app.instance_path) / "uploads" / "chats" / str(msg.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        for upload in uploads:
            original_name = clean_original_filename(upload.filename)
            if not original_name or not is_allowed_attachment(original_name):
                db.session.rollback(); abort(400)
            stored_name = random_storage_name(uuid.uuid4().hex, original_name)
            target = upload_dir / stored_name
            upload.save(str(target))
            size = target.stat().st_size
            if size > MAX_ATTACHMENT_BYTES:
                target.unlink(missing_ok=True); db.session.rollback(); flash("حجم المرفق يتجاوز 25 م.ب.", "danger"); return redirect(url_for("chats.conversation", conversation_id=conversation_id))
            db.session.add(ChatAttachment(message_id=msg.id, original_name=original_name, stored_name=stored_name, mime_type=(upload.mimetype or mimetypes.guess_type(original_name)[0] or "application/octet-stream")[:120], file_size=size))
        for row in conversation.participants:
            if row.user_id != current_user.id and not row.is_muted:
                emit_event(actor_id=current_user.id, action="CHAT_MESSAGE_SENT", message="رسالة محادثة جديدة", target_type="ChatConversation", target_id=conversation_id, notify_user_id=row.user_id, level="INFO", auto_commit=False)
        db.session.commit()
        return redirect(url_for("chats.conversation", conversation_id=conversation_id))

    membership.last_read_at = datetime.utcnow()
    db.session.commit()
    messages = ChatMessage.query.filter_by(conversation_id=conversation_id).order_by(ChatMessage.created_at.asc()).all()
    read_by_all = {
        message.id: all(p.user_id == message.sender_id or (p.last_read_at and p.last_read_at >= message.created_at) for p in conversation.participants)
        for message in messages if message.sender_id == current_user.id
    }
    return render_template("chats/conversation.html", conversation=conversation, messages=messages, read_by_all=read_by_all, is_muted=membership.is_muted, is_pinned=membership.is_pinned, can_manage_group=(conversation.kind == "GROUP" and conversation.created_by_id == current_user.id), eligible_users=[u for u in User.query.order_by(User.name.asc()).all() if u.id != current_user.id and u.has_perm(CHAT_ACCESS)])


@chats_bp.route("/attachment/<int:attachment_id>")
@login_required
@chat_access_required
def download_attachment(attachment_id):
    attachment = ChatAttachment.query.get_or_404(attachment_id)
    if not _participant(attachment.message.conversation_id):
        abort(403)
    folder = Path(current_app.instance_path) / "uploads" / "chats" / str(attachment.message_id)
    return send_from_directory(str(folder), attachment.stored_name, as_attachment=not is_safe_inline_mimetype(attachment.mime_type), download_name=attachment.original_name, mimetype=attachment.mime_type or None)
