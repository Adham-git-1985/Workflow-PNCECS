from flask import render_template, request
from flask_login import login_required
from datetime import datetime, timedelta
from sqlalchemy import or_, func

from . import audit_bp
from models import AuditLog, User
from extensions import db
from permissions import roles_required


@audit_bp.route("/")
@login_required
@roles_required("ADMIN")
def audit_index():

    page = request.args.get("page", 1, type=int)

    pagination = (
        AuditLog.query
        .order_by(AuditLog.created_at.desc())
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

    query = AuditLog.query.outerjoin(User)

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
                    User.name.ilike(f"%{word}%")
                )
            )

    page = request.args.get("page", 1, type=int)

    pagination = query.order_by(
        AuditLog.created_at.desc()
    ).paginate(page=page, per_page=20, error_out=False)

    users = User.query.all()

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

    total_logs = AuditLog.query.count()

    top_users = (
        db.session.query(
            User.email,
            func.count(AuditLog.id)
        )
        .join(AuditLog, AuditLog.user_id == User.id)
        .group_by(User.email)
        .order_by(func.count(AuditLog.id).desc())
        .limit(5)
        .all()
    )

    top_actions = (
        db.session.query(
            AuditLog.action,
            func.count(AuditLog.id)
        )
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

    action = request.args.get("action")
    user_id = request.args.get("user_id")

    query = AuditLog.query

    if action:
        query = query.filter(AuditLog.action == action)

    if user_id:
        query = query.filter(AuditLog.user_id == user_id)

    logs = (
        AuditLog.query
            .order_by(AuditLog.created_at.desc())
            .limit(200)
            .all()
    )

    users = User.query.all()

    return render_template(
        "audit/timeline.html",
        logs=logs,
        users=users
    )
