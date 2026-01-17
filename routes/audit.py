from flask import render_template, request
from flask_login import login_required
from models import AuditLog, User
from datetime import datetime, timedelta
from sqlalchemy import or_
from extensions import db
from utils.permissions import admin_required
from flask import Blueprint


audit_bp = Blueprint(
    "audit",
    __name__,
    url_prefix="/audit"
)

@audit_bp.route("/audit-logs")
@login_required
@admin_required
def list_audit_logs():

    user_id = request.args.get("user_id")
    action = request.args.get("action")
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")

    query = AuditLog.query

    search = request.args.get("search")


    # 🔹 فلترة حسب المستخدم
    if user_id:
        query = query.filter(AuditLog.user_id == user_id)

    # 🔹 فلترة حسب نوع الإجراء
    if action:
        query = query.filter(AuditLog.action == action)

    # 🔹 فلترة حسب التاريخ
    if date_from:
        query = query.filter(
            AuditLog.created_at >= datetime.strptime(date_from, "%Y-%m-%d")
        )

    if date_to:
        query = query.filter(
            AuditLog.created_at < (
                    datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
            )
        )

    # 🔍 بحث نصي
    if search:
        keywords = search.strip().split()

        # نعمل join فقط إذا احتجنا اسم المستخدم
        query = query.outerjoin(User)

        for word in keywords:
            query = query.filter(
                or_(
                    AuditLog.note.ilike(f"%{word}%"),
                    AuditLog.action.ilike(f"%{word}%"),
                    User.name.ilike(f"%{word}%")
                )
            )

    # رقم الصفحة (يأتي من الرابط ?page=1)
    page = request.args.get("page", 1, type=int)

    # إنشاء Pagination
    pagination = query.order_by(
        AuditLog.created_at.desc()
    ).paginate(
        page=page,
        per_page=20,  # عدد السجلات في كل صفحة
        error_out=False
    )

    # السجلات الخاصة بالصفحة الحالية فقط
    logs = pagination.items

    users = User.query.all()

    search_terms = []
    if search:
        search_terms = search.strip().split()

    return render_template(
        "audit/list.html",
        logs=logs,
        users=users,
        pagination=pagination,
        search_terms=search_terms
    )


from sqlalchemy import func

@audit_bp.route("/audit-dashboard")
@login_required
@admin_required
def audit_dashboard():

    # 1️⃣ عدد العمليات الكلي
    total_logs = AuditLog.query.count()

    # 2️⃣ أكثر المستخدمين نشاطًا
    top_users = (
        db.session.query(
            User.email,
            func.count(AuditLog.id).label("count")
        )
            .join(AuditLog, AuditLog.user_id == User.id)
            .group_by(User.email)
            .order_by(func.count(AuditLog.id).desc())
            .limit(5)
            .all()
    )

    # 3️⃣ أكثر الإجراءات
    top_actions = (
        db.session.query(
            AuditLog.action,
            func.count(AuditLog.id).label("count")
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



