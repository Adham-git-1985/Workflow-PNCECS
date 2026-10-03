from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
import mimetypes
import uuid

from flask import abort, current_app, flash, jsonify, redirect, render_template, request, send_from_directory, url_for
from flask_login import current_user, login_required
from sqlalchemy import and_, func, or_

from extensions import db
from models import AuditLog, ChatAttachment, ChatConversation, ChatMessage, ChatParticipant, ChatTyping, User, UserPermission, WorkflowInstance, WorkflowStepTask, WorkflowRequest
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


def _explicit_chat_user_ids():
    return {uid for (uid,) in db.session.query(UserPermission.user_id).filter(UserPermission.key == CHAT_ACCESS, UserPermission.is_allowed.is_(True)).all()}


def _direct_conversation(other_user_id):
    mine = db.session.query(ChatParticipant.conversation_id).filter_by(user_id=current_user.id)
    return (ChatConversation.query.join(ChatParticipant)
            .filter(ChatConversation.kind == "DIRECT", ChatConversation.id.in_(mine))
            .filter(ChatConversation.id.in_(db.session.query(ChatParticipant.conversation_id).filter_by(user_id=other_user_id)))
            .first())


def _unread_count(user_id):
    """Return the number of conversations containing unread messages."""
    return len(_unread_by_conversation(user_id))


def _unread_by_conversation(user_id, conversation_ids=None):
    """Return ``{conversation_id: unread_message_count}`` for one user.

    The header intentionally counts conversations, while the inbox needs the
    exact number of new messages in each conversation so it can make the
    unread state obvious to the user.
    """
    query = (
        db.session.query(ChatMessage.conversation_id, func.count(ChatMessage.id))
        .join(
            ChatParticipant,
            and_(
                ChatParticipant.conversation_id == ChatMessage.conversation_id,
                ChatParticipant.user_id == user_id,
            ),
        )
        .filter(
            ChatMessage.sender_id != user_id,
            ChatMessage.is_deleted.is_(False),
            or_(
                ChatParticipant.last_read_at.is_(None),
                ChatMessage.created_at > ChatParticipant.last_read_at,
            ),
        )
    )
    if conversation_ids is not None:
        conversation_ids = list(conversation_ids)
        if not conversation_ids:
            return {}
        query = query.filter(ChatMessage.conversation_id.in_(conversation_ids))

    return {
        conversation_id: int(message_count)
        for conversation_id, message_count in query.group_by(ChatMessage.conversation_id).all()
    }


def _message_receipt_status(conversation, message):
    """Return WhatsApp-style receipt status for the message sender.

    For groups, the status advances only after every other current participant
    has received/read the message.  Direct conversations therefore map exactly
    to one tick (sent), two grey ticks (delivered), and two blue ticks (read).
    """
    recipients = [
        participant
        for participant in conversation.participants
        if participant.user_id != message.sender_id
    ]
    if not recipients:
        return "sent"

    delivered_to_all = all(
        participant.last_delivered_at
        and participant.last_delivered_at >= message.created_at
        for participant in recipients
    )
    if not delivered_to_all:
        return "sent"

    read_by_all = all(
        participant.last_read_at and participant.last_read_at >= message.created_at
        for participant in recipients
    )
    return "read" if read_by_all else "delivered"


def _record_delivery_for_current_user(delivered_by_conversation):
    """Persist the latest message arrival acknowledged by this browser."""
    if not delivered_by_conversation:
        return

    memberships = ChatParticipant.query.filter(
        ChatParticipant.user_id == current_user.id,
        ChatParticipant.conversation_id.in_(delivered_by_conversation),
    ).all()
    changed = False
    for membership in memberships:
        delivered_at = delivered_by_conversation.get(membership.conversation_id)
        if delivered_at and (
            not membership.last_delivered_at
            or membership.last_delivered_at < delivered_at
        ):
            membership.last_delivered_at = delivered_at
            changed = True
    if changed:
        db.session.commit()


def _message_fragment(conversation, message):
    return render_template(
        "chats/_message.html",
        conversation=conversation,
        message=message,
        message_receipt_status=_message_receipt_status(conversation, message),
    )


def _attachment_file_path(message_id, stored_name):
    return (
        Path(current_app.instance_path)
        / "uploads"
        / "chats"
        / str(message_id)
        / Path(stored_name).name
    )


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
        matching_ids = db.session.query(ChatMessage.conversation_id).filter(
            ChatMessage.is_deleted.is_(False),
            ChatMessage.body.ilike(needle),
        )
        conversations = [c for c in conversations if c.id in set(cid for (cid,) in matching_ids.all()) or search.lower() in (c.title or "").lower()]
    users = User.query.order_by(User.name.asc(), User.email.asc()).all()
    explicit_ids = _explicit_chat_user_ids()
    eligible_users = [u for u in users if u.id != current_user.id and u.id in explicit_ids]
    unread_by_conversation = _unread_by_conversation(
        current_user.id, [conversation.id for conversation in conversations]
    )
    return render_template(
        "chats/inbox.html",
        conversations=conversations,
        eligible_users=eligible_users,
        unread_count=len(unread_by_conversation),
        unread_by_conversation=unread_by_conversation,
        q=search,
    )


@chats_bp.route("/unread-count")
@login_required
@chat_access_required
def unread_count():
    unread_by_conversation = _unread_by_conversation(current_user.id)
    return jsonify({
        "count": len(unread_by_conversation),
        "messages": sum(unread_by_conversation.values()),
    })


@chats_bp.route("/alerts")
@login_required
@chat_access_required
def chat_alerts():
    """Return incoming chat event ids for the private chat sound channel.

    This deliberately reads from chat participation rather than the general
    Notification table, so chat messages never enter Masar's notification
    centre or use its notification sound.
    """
    after_id = request.args.get("after_id", type=int)
    incoming = (
        db.session.query(ChatMessage.id, ChatMessage.conversation_id, ChatMessage.created_at)
        .join(ChatParticipant, ChatParticipant.conversation_id == ChatMessage.conversation_id)
        .filter(
            ChatParticipant.user_id == current_user.id,
            ChatParticipant.is_muted.is_(False),
            ChatMessage.sender_id != current_user.id,
            ChatMessage.is_deleted.is_(False),
        )
    )

    events = []
    if after_id is None:
        # The first request establishes a cursor and must not sound old
        # messages when a user first opens a page, but it does acknowledge
        # their delivery to update the sender's receipt correctly.
        delivered_rows = (
            db.session.query(ChatMessage.conversation_id, func.max(ChatMessage.created_at))
            .join(ChatParticipant, ChatParticipant.conversation_id == ChatMessage.conversation_id)
            .filter(
                ChatParticipant.user_id == current_user.id,
                ChatMessage.sender_id != current_user.id,
                ChatMessage.is_deleted.is_(False),
            )
            .group_by(ChatMessage.conversation_id)
            .all()
        )
        _record_delivery_for_current_user(dict(delivered_rows))
        cursor = incoming.with_entities(func.max(ChatMessage.id)).scalar() or 0
    else:
        rows = (
            incoming
            .filter(ChatMessage.id > max(int(after_id), 0))
            .order_by(ChatMessage.id.asc())
            .limit(50)
            .all()
        )
        events = [
            {"message_id": int(message_id), "conversation_id": int(conversation_id)}
            for message_id, conversation_id, _created_at in rows
        ]
        delivered_by_conversation = {}
        for _message_id, conversation_id, created_at in rows:
            previous = delivered_by_conversation.get(conversation_id)
            if not previous or created_at > previous:
                delivered_by_conversation[conversation_id] = created_at
        _record_delivery_for_current_user(delivered_by_conversation)
        cursor = events[-1]["message_id"] if events else max(int(after_id), 0)

    response = jsonify({"cursor": int(cursor), "events": events})
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@chats_bp.route("/<int:conversation_id>/typing", methods=["POST"])
@login_required
@chat_access_required
def typing(conversation_id):
    if not _participant(conversation_id):
        abort(403)
    active_value = str(request.values.get("active", "1")).strip().lower()
    is_active = active_value not in {"0", "false", "no", "off"}
    row = ChatTyping.query.filter_by(conversation_id=conversation_id, user_id=current_user.id).first()
    if not is_active:
        if row:
            db.session.delete(row)
            db.session.commit()
        return jsonify({"ok": True, "active": False})
    if not row:
        row = ChatTyping(conversation_id=conversation_id, user_id=current_user.id)
        db.session.add(row)
    row.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"ok": True, "active": True})


@chats_bp.route("/<int:conversation_id>/typing/status")
@login_required
@chat_access_required
def typing_status(conversation_id):
    if not _participant(conversation_id):
        abort(403)
    # Do not rely on a lazy relationship here: this endpoint is polled while
    # another user is typing, so it must work even on an already-running app
    # process that loaded the older ChatTyping model definition.
    cutoff = datetime.utcnow() - timedelta(seconds=12)
    users = (
        User.query
        .join(ChatTyping, ChatTyping.user_id == User.id)
        .filter(
            ChatTyping.conversation_id == conversation_id,
            ChatTyping.user_id != current_user.id,
            ChatTyping.updated_at >= cutoff,
        )
        .all()
    )
    response = jsonify({"users": [user.full_name for user in users]})
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


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
    explicit_ids = _explicit_chat_user_ids()
    if any(u.id not in explicit_ids and u.id != current_user.id for u in users):
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
    explicit_ids = _explicit_chat_user_ids()
    if len(users) < 3 or any(user.id not in explicit_ids and user.id != current_user.id for user in users):
        flash("اختر مستخدمين مخوّلين اثنين على الأقل للمجموعة.", "warning")
        return redirect(url_for("chats.inbox"))
    conversation = ChatConversation(title=title or "مجموعة جديدة", kind="GROUP", created_by_id=current_user.id)
    db.session.add(conversation); db.session.flush()
    db.session.add_all([ChatParticipant(conversation_id=conversation.id, user_id=user.id) for user in users])
    for user in users:
        if user.id != current_user.id:
            emit_event(actor_id=current_user.id, action="CHAT_STARTED", message="تمت إضافتك إلى مجموعة محادثة جديدة", target_type="ChatConversation", target_id=conversation.id, notify_user_id=user.id, level="INFO", auto_commit=False)
    db.session.commit()
    return redirect(url_for("chats.conversation", conversation_id=conversation.id))


@chats_bp.route("/direct/<int:user_id>", methods=["POST"])
@login_required
@chat_access_required
def start_direct(user_id):
    other = User.query.get_or_404(user_id)
    if other.id == current_user.id or other.id not in _explicit_chat_user_ids():
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
        emit_event(actor_id=current_user.id, action="CHAT_STARTED", message="بدأ محادثة جديدة معك", target_type="ChatConversation", target_id=conversation.id, notify_user_id=other.id, level="INFO", auto_commit=False)
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
        allowed_ids = {u.id for u in User.query.filter(User.id.in_(participant_ids)).all() if u.id in _explicit_chat_user_ids()}
        allowed_ids.add(current_user.id)
        conversation = ChatConversation(title=f"محادثة الطلب #{req.id}", kind="WORKFLOW", workflow_request_id=req.id, created_by_id=current_user.id)
        db.session.add(conversation)
        db.session.flush()
        db.session.add_all([ChatParticipant(conversation_id=conversation.id, user_id=uid) for uid in allowed_ids])
        db.session.commit()
    if not ChatParticipant.query.filter_by(conversation_id=conversation.id, user_id=current_user.id).first():
        abort(403)
    return redirect(url_for("chats.conversation", conversation_id=conversation.id))


@chats_bp.route("/<int:conversation_id>/updates")
@login_required
@chat_access_required
def message_updates(conversation_id):
    """Return only the new (or subsequently deleted) messages for an open chat.

    The browser uses this as a small, reliable fallback when a corporate proxy
    buffers the global EventSource connection.  It never exposes a message to
    anyone who is not already a participant in its conversation.
    """
    conversation = ChatConversation.query.get_or_404(conversation_id)
    membership = _participant(conversation_id)
    if not membership:
        abort(403)

    after_id = max(0, request.args.get("after_id", type=int) or 0)
    rows = (
        ChatMessage.query
        .filter(
            ChatMessage.conversation_id == conversation_id,
            or_(ChatMessage.id > after_id, ChatMessage.is_deleted.is_(True)),
        )
        .order_by(ChatMessage.created_at.asc())
        .all()
    )
    # The normal page view records the initial read.  Subsequent short polls
    # only write when a newly delivered, non-deleted message actually arrived.
    if any(row.sender_id != current_user.id and not row.is_deleted for row in rows):
        received_at = datetime.utcnow()
        membership.last_delivered_at = received_at
        membership.last_read_at = received_at
        db.session.commit()
    last_id = max([after_id, *[row.id for row in rows]])
    receipt_messages = (
        ChatMessage.query
        .filter(
            ChatMessage.conversation_id == conversation_id,
            ChatMessage.sender_id == current_user.id,
            ChatMessage.is_deleted.is_(False),
        )
        .order_by(ChatMessage.id.desc())
        .limit(100)
        .all()
    )
    response = jsonify({
        "messages": [
            {"id": row.id, "deleted": bool(row.is_deleted), "html": _message_fragment(conversation, row)}
            for row in rows
        ],
        "last_id": last_id,
        "receipts": [
            {"message_id": message.id, "status": _message_receipt_status(conversation, message)}
            for message in receipt_messages
        ],
    })
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


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
        # Chat delivery is intentionally separate from the global Notification
        # table.  Recipients receive the private chat sound/event through the
        # chat alerts endpoint, while the header chat badge reflects unread
        # conversations without polluting Masar's notification centre.
        typing_row = ChatTyping.query.filter_by(conversation_id=conversation_id, user_id=current_user.id).first()
        if typing_row:
            db.session.delete(typing_row)
        db.session.commit()
        return redirect(url_for("chats.conversation", conversation_id=conversation_id))

    opened_at = datetime.utcnow()
    membership.last_delivered_at = opened_at
    membership.last_read_at = opened_at
    db.session.commit()
    messages = ChatMessage.query.filter_by(conversation_id=conversation_id).order_by(ChatMessage.created_at.asc()).all()
    receipt_statuses = {
        message.id: _message_receipt_status(conversation, message)
        for message in messages if message.sender_id == current_user.id
    }
    explicit_ids = _explicit_chat_user_ids()
    direct_peer = next((p.user for p in conversation.participants if p.user_id != current_user.id), None) if conversation.kind == "DIRECT" else None
    return render_template("chats/conversation.html", conversation=conversation, messages=messages, receipt_statuses=receipt_statuses, is_muted=membership.is_muted, is_pinned=membership.is_pinned, direct_peer=direct_peer, can_manage_group=(conversation.kind == "GROUP" and conversation.created_by_id == current_user.id), eligible_users=[u for u in User.query.order_by(User.name.asc()).all() if u.id != current_user.id and u.id in explicit_ids])


@chats_bp.route("/attachment/<int:attachment_id>")
@login_required
@chat_access_required
def download_attachment(attachment_id):
    attachment = ChatAttachment.query.get_or_404(attachment_id)
    if attachment.message.is_deleted or not _participant(attachment.message.conversation_id):
        abort(403)
    folder = Path(current_app.instance_path) / "uploads" / "chats" / str(attachment.message_id)
    return send_from_directory(str(folder), attachment.stored_name, as_attachment=not is_safe_inline_mimetype(attachment.mime_type), download_name=attachment.original_name, mimetype=attachment.mime_type or None)


@chats_bp.route("/attachment/<int:attachment_id>/delete", methods=["POST"])
@login_required
@chat_access_required
def delete_attachment(attachment_id):
    attachment = ChatAttachment.query.get_or_404(attachment_id)
    message = attachment.message
    if message.is_deleted or message.sender_id != current_user.id or not _participant(message.conversation_id):
        abort(403)
    file_path = _attachment_file_path(message.id, attachment.stored_name)
    db.session.delete(attachment)
    db.session.commit()
    try:
        file_path.unlink(missing_ok=True)
    except OSError:
        current_app.logger.warning("Could not remove chat attachment %s", file_path)
    return redirect(url_for("chats.conversation", conversation_id=message.conversation_id))


@chats_bp.route("/message/<int:message_id>/delete", methods=["POST"])
@login_required
@chat_access_required
def delete_message(message_id):
    message = ChatMessage.query.get_or_404(message_id)
    if message.sender_id != current_user.id or not _participant(message.conversation_id):
        abort(403)
    if message.is_deleted:
        return redirect(url_for("chats.conversation", conversation_id=message.conversation_id))

    attachment_paths = [
        _attachment_file_path(message.id, attachment.stored_name)
        for attachment in message.attachments
    ]
    for attachment in list(message.attachments):
        db.session.delete(attachment)
    message.is_deleted = True
    message.deleted_at = datetime.utcnow()
    db.session.add(AuditLog(
        user_id=current_user.id,
        action="CHAT_MESSAGE_DELETED",
        target_type="ChatMessage",
        target_id=message.id,
        note=f"conversation={message.conversation_id}",
    ))
    db.session.commit()
    for file_path in attachment_paths:
        try:
            file_path.unlink(missing_ok=True)
        except OSError:
            current_app.logger.warning("Could not remove chat attachment %s", file_path)
    return redirect(url_for("chats.conversation", conversation_id=message.conversation_id))
