from datetime import datetime
from functools import wraps

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import and_, func

from extensions import db
from models import ChatConversation, ChatMessage, ChatParticipant, User, WorkflowInstance, WorkflowStepTask, WorkflowRequest
from utils.events import emit_event
from . import chats_bp

CHAT_ACCESS = "CHAT_ACCESS"


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


@chats_bp.route("/")
@login_required
@chat_access_required
def inbox():
    conversations = (ChatConversation.query.join(ChatParticipant)
        .filter(ChatParticipant.user_id == current_user.id)
        .order_by(ChatConversation.updated_at.desc()).all())
    users = User.query.order_by(User.name.asc(), User.email.asc()).all()
    eligible_users = [u for u in users if u.id != current_user.id and u.has_perm(CHAT_ACCESS)]
    return render_template("chats/inbox.html", conversations=conversations, eligible_users=eligible_users)


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
        if not body:
            flash("اكتب رسالة أولاً.", "warning")
            return redirect(url_for("chats.conversation", conversation_id=conversation_id))
        msg = ChatMessage(conversation_id=conversation_id, sender_id=current_user.id, body=body)
        conversation.updated_at = datetime.utcnow()
        db.session.add(msg)
        for row in conversation.participants:
            if row.user_id != current_user.id and not row.is_muted:
                emit_event(actor_id=current_user.id, action="CHAT_MESSAGE_SENT", message="رسالة محادثة جديدة", target_type="ChatConversation", target_id=conversation_id, notify_user_id=row.user_id, level="INFO", auto_commit=False)
        db.session.commit()
        return redirect(url_for("chats.conversation", conversation_id=conversation_id))

    membership.last_read_at = datetime.utcnow()
    db.session.commit()
    messages = ChatMessage.query.filter_by(conversation_id=conversation_id).order_by(ChatMessage.created_at.asc()).all()
    return render_template("chats/conversation.html", conversation=conversation, messages=messages)
