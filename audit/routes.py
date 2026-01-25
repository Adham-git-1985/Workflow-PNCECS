from flask import render_template, request
from flask_login import login_required, current_user
from datetime import datetime, timedelta
from sqlalchemy import or_, func

from . import audit_bp
from models import AuditLog, User
from extensions import db
from permissions import roles_required


def _apply_message_visibility_filter(query):
    """Hide MESSAGE_* audit entries for non-SUPER_ADMIN."""
    role = (getattr(current_user, "role", "") or "").strip().upper()
    if role != "SUPER_ADMIN":
        query = query.filter(~AuditLog.action.like("MESSAGE_%"))
    return query


@audit_bp.route("/")
@login_required
@roles_required("ADMIN")
def audit_index():
    page = request.args.get("page", 1, type=int)

    q = _apply_message_visibility_filter(AuditLog.query)

    pagination = (
        q.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .paginate(page=page, per_page=20, error_out=False)
    )

    return render_template(
        "audit/index.html",
        logs=pagination.items,
        pagination=pagination
    )


@audit_bp.route("/logs")
@login_required
@roles_required("ADMIN")
def list_audit_logs():
    user_id = request.args.get("user_id")
    action = request.args.get("action")
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    search = request.args.get("search")

    query = _apply_message_visibility_filter(AuditLog.query.outerjoin(User))

    if user_id:
        query = query.filter(AuditLog.user_id == user_id)

    if action:
        query = query.filter(AuditLog.action == action)

    if date_from:
        query = query.filter(
            AuditLog.created_at >= datetime.strptime(date_from, "%Y-%m-%d")
        )

    if date_to:
        query = query.filter(
            AuditLog.created_at <
            datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
        )

    if search:
        for word in search.strip().split():
            query = query.filter(
                or_(
                    AuditLog.note.ilike(f"%{word}%"),
                    AuditLog.action.ilike(f"%{word}%"),
                    User.email.ilike(f"%{word}%")
                )
            )

    page = request.args.get("page", 1, type=int)

    pagination = query.order_by(
        AuditLog.created_at.desc(), AuditLog.id.desc()
    ).paginate(page=page, per_page=20, error_out=False)

    users = User.query.order_by(User.email.asc()).all()

    return render_template(
        "audit/list.html",
        logs=pagination.items,
        users=users,
        pagination=pagination
    )


@audit_bp.route("/dashboard")
@login_required
@roles_required("ADMIN")
def audit_dashboard():
    q = _apply_message_visibility_filter(AuditLog.query)

    total_logs = q.count()

    top_users = (
        db.session.query(
            User.email,
            func.count(AuditLog.id)
        )
        .join(AuditLog, AuditLog.user_id == User.id)
    )

    # Hide message logs for non-super admin
    if (getattr(current_user, "role", "") or "").strip().upper() != "SUPER_ADMIN":
        top_users = top_users.filter(~AuditLog.action.like("MESSAGE_%"))

    top_users = (
        top_users
        .group_by(User.email)
        .order_by(func.count(AuditLog.id).desc())
        .limit(5)
        .all()
    )

    top_actions_q = db.session.query(
        AuditLog.action,
        func.count(AuditLog.id)
    )
    if (getattr(current_user, "role", "") or "").strip().upper() != "SUPER_ADMIN":
        top_actions_q = top_actions_q.filter(~AuditLog.action.like("MESSAGE_%"))

    top_actions = (
        top_actions_q
        .group_by(AuditLog.action)
        .order_by(func.count(AuditLog.id).desc())
        .limit(5)
        .all()
    )

    return render_template(
        "audit/dashboard.html",
        total_logs=total_logs,
        top_users=top_users,
        top_actions=top_actions
    )


@audit_bp.route("/timeline")
@login_required
@roles_required("ADMIN")
def system_timeline():
    """High-volume timeline with date range + pagination.

    Default: last 7 days.
    """

    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 120, type=int)
    per_page = max(50, min(per_page, 500))

    action = request.args.get("action")
    user_id = request.args.get("user_id", type=int)
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    days = request.args.get("days", type=int)

    base = _apply_message_visibility_filter(AuditLog.query.outerjoin(User))

    if action:
        base = base.filter(AuditLog.action == action)

    if user_id:
        base = base.filter(AuditLog.user_id == user_id)

    # Default time window
    if not date_from and not date_to and not days:
        days = 7

    if days:
        base = base.filter(AuditLog.created_at >= datetime.utcnow() - timedelta(days=days))

    if date_from:
        base = base.filter(AuditLog.created_at >= datetime.strptime(date_from, "%Y-%m-%d"))

    if date_to:
        base = base.filter(
            AuditLog.created_at < datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
        )

    # Summary by day (for quick navigation)
    try:
        day_label = func.strftime('%Y-%m-%d', AuditLog.created_at)
        day_counts = (
            base.with_entities(day_label.label('day'), func.count(AuditLog.id))
            .group_by('day')
            .order_by(day_label.desc())
            .limit(31)
            .all()
        )
    except Exception:
        day_counts = []

    pagination = (
        base.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .paginate(page=page, per_page=per_page, error_out=False)
    )

    users = User.query.order_by(User.email.asc()).all()

    # Action dropdown: keep it light
    actions_q = db.session.query(AuditLog.action).distinct().order_by(AuditLog.action)
    actions_q = _apply_message_visibility_filter(actions_q)
    actions = [a for (a,) in actions_q.limit(200).all()]

    return render_template(
        "audit/timeline.html",
        logs=pagination.items,
        pagination=pagination,
        users=users,
        actions=actions,
        day_counts=day_counts,
        filters={
            "action": action or "",
            "user_id": user_id or "",
            "date_from": date_from or "",
            "date_to": date_to or "",
            "days": days or "",
            "per_page": per_page,
        }
    )
