from flask import Flask, render_template, request, redirect, url_for, flash, session
from extensions import db
from models import Approval, WorkflowRequest, User
from functools import wraps
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from flask import send_file
import io
from routes.audit import audit_bp
from flask_migrate import Migrate
from extensions import db, login_manager
from models import User
from flask import request
from urllib.parse import urlparse
from werkzeug.security import check_password_hash, generate_password_hash
from flask_login import login_user, login_required, current_user, logout_user, UserMixin
from routes.users import users_bp
from sqlalchemy import func
from datetime import datetime




app = Flask(__name__)
app.config["SECRET_KEY"] = "super-secret-key-change-this"


STATUS_ROLE_MAP = {
    "SUBMITTED": "dept_head",
    "DEPT_REVIEW": "finance",
    "FIN_REVIEW": "secretary_general"
}

NEXT_STATUS_MAP = {
    "SUBMITTED": "DEPT_REVIEW",
    "DEPT_REVIEW": "FIN_REVIEW",
    "FIN_REVIEW": "APPROVED"
}

REJECT_STATUS = "REJECTED"


app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///workflow.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

app.register_blueprint(audit_bp)
app.register_blueprint(users_bp)

db.init_app(app)
login_manager.init_app(app)
login_manager.login_view = "login"  # أو اسم صفحة تسجيل الدخول عندك


migrate = Migrate(app, db)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))



@app.route("/login", methods=["GET", "POST"], strict_slashes=False)
def login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        user = User.query.filter_by(email=email).first()

        if user and check_password_hash(user.password_hash, password):
            login_user(user, remember=True)


            next_page = request.args.get("next")

            # حماية من open redirect
            if not next_page or urlparse(next_page).netloc != "":
                next_page = url_for("inbox")

            return redirect(next_page)

        flash("بيانات الدخول غير صحيحة", "danger")

    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/request/new", methods=["GET", "POST"])
def create_request():
    if request.method == "POST":
        title = request.form.get("title")
        description = request.form.get("description")
        action = request.form.get("action")

        status = "DRAFT" if action == "save" else "SUBMITTED"

        requester = User.query.first()

        new_request = WorkflowRequest(
            title=title,
            description=description,
            status=status,
            requester_id=requester.id
        )

        old_status = None
        new_status = status

        db.session.add(new_request)
        db.session.flush()

        log_action(
            request_obj=new_request,
            user=current_user,
            action="CREATE_REQUEST",
            old_status=old_status,
            new_status=new_status,
            note="تم إنشاء الطلب"
        )

        db.session.commit()

        flash("تم حفظ الطلب بنجاح")
        return redirect(url_for("create_request"))

    return render_template(
        "create_request.html",
        breadcrumb=[
            {"label": "Inbox", "url": url_for("inbox")},
            {"label": "طلب جديد"}
        ]
    )


@app.route("/my-requests")
@login_required
def my_requests():

    status = request.args.get("status")

    # 🔹 Query أساسي لكل طلبات المستخدم
    base_query = WorkflowRequest.query.filter_by(
        requester_id=current_user.id
    )

    # 🔹 حساب العدّادات (بدون فلترة)
    total = base_query.count()

    approved = base_query.filter(
        WorkflowRequest.status == "APPROVED"
    ).count()

    rejected = base_query.filter(
        WorkflowRequest.status == "REJECTED"
    ).count()

    draft = base_query.filter(
        WorkflowRequest.status == "DRAFT"
    ).count()

    in_progress = base_query.filter(
        WorkflowRequest.status.notin_(["APPROVED", "REJECTED", "DRAFT"])
    ).count()

    # 🔹 فلترة الجدول فقط حسب الضغط على الـ Counter
    if status == "approved":
        base_query = base_query.filter(WorkflowRequest.status == "APPROVED")

    elif status == "rejected":
        base_query = base_query.filter(WorkflowRequest.status == "REJECTED")

    elif status == "draft":
        base_query = base_query.filter(WorkflowRequest.status == "DRAFT")

    elif status == "in_progress":
        base_query = base_query.filter(
            WorkflowRequest.status.notin_(["APPROVED", "REJECTED", "DRAFT"])
        )

    requests = base_query.order_by(
        WorkflowRequest.id.desc()
    ).all()

    return render_template(
        "my_requests.html",
        requests=requests,
        counters={
            "total": total,
            "approved": approved,
            "rejected": rejected,
            "draft": draft,
            "in_progress": in_progress
        },
        last_update=datetime.utcnow(),
        breadcrumb=[
            {"label": "Inbox", "url": url_for("inbox")},
            {"label": "طلباتي"}
        ]
    )


@app.route("/inbox")
@login_required
def inbox():
    #current_user = get_current_user()

    allowed_statuses = [
        status for status, role in STATUS_ROLE_MAP.items()
        if role == current_user.role
    ]

    requests = WorkflowRequest.query.filter(
        WorkflowRequest.status.in_(allowed_statuses)
    ).all()

    return render_template(
        "inbox.html",
        requests=requests,
        breadcrumb=[
            {"label": "Inbox", "url": url_for("inbox")}
        ]
    )


@app.route("/request/<int:request_id>")
def review_request(request_id):
    req = WorkflowRequest.query.get_or_404(request_id)
    return render_template(
        "review_request.html",
        req=req,
        breadcrumb=[
            {"label": "Inbox", "url": url_for("inbox")},
            {"label": f"Request #{req.id}", "url": url_for("review_request", request_id=req.id)},
            {"label": "Review"}
        ]
    )


@app.route("/request/<int:request_id>/action", methods=["POST"])
@login_required
def request_action(request_id):
    req = WorkflowRequest.query.get_or_404(request_id)
    current_user = get_current_user()

    action = request.form.get("action")
    note = request.form.get("note")

    old_status = req.status

    if action == "approve":
        req.status = NEXT_STATUS_MAP.get(req.status, req.status)
        action_name = "APPROVE"
    elif action == "reject":
        req.status = REJECT_STATUS
        action_name = "REJECT"

    log_action(
        request_obj=req,
        user=current_user,
        action=action_name,
        old_status=old_status,
        new_status=req.status,
        note=note
    )

    approval = Approval(
        request_id=req.id,
        user_id=current_user.id,
        action=action,
        note=note
    )

    db.session.add(approval)
    db.session.commit()

    flash("تم تسجيل الإجراء بنجاح", "success")
    return redirect(url_for("inbox"))

from models import AuditLog

def log_action(request_obj, user, action, old_status, new_status, note=None):
    log = AuditLog(
        request_id=request_obj.id,
        user_id=user.id if user else None,
        action=action,
        old_status=old_status,
        new_status=new_status,
        note=note
    )
    db.session.add(log)

@app.route("/request/<int:request_id>/audit")
@login_required
def request_audit(request_id):
    logs = AuditLog.query.filter_by(
        request_id=request_id
    ).order_by(AuditLog.created_at.asc()).all()

    req = WorkflowRequest.query.get_or_404(request_id)

    return render_template(
        "audit_log.html",
        logs=logs,
        req=req,
        breadcrumb=[
            {"label": "Inbox", "url": url_for("inbox")},
            {"label": f"Request #{req.id}", "url": url_for("review_request", request_id=req.id)},
            {"label": "Audit Log"}
        ]
    )


@app.route("/request/<int:request_id>/pdf")
@login_required
def request_pdf(request_id):
    req = WorkflowRequest.query.get_or_404(request_id)
    logs = AuditLog.query.filter_by(
        request_id=request_id
    ).order_by(AuditLog.created_at.asc()).all()

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)

    styles = getSampleStyleSheet()
    elements = []

    # العنوان
    elements.append(Paragraph("<b>تقرير مسار الطلب</b>", styles["Title"]))
    elements.append(Spacer(1, 12))

    # بيانات الطلب
    elements.append(Paragraph(f"<b>رقم الطلب:</b> {req.id}", styles["Normal"]))
    elements.append(Paragraph(f"<b>العنوان:</b> {req.title}", styles["Normal"]))
    elements.append(Paragraph(f"<b>الوصف:</b> {req.description}", styles["Normal"]))
    elements.append(Paragraph(f"<b>الحالة الحالية:</b> {req.status}", styles["Normal"]))
    elements.append(Spacer(1, 12))

    # جدول السجل
    table_data = [
        ["التاريخ", "المستخدم", "الإجراء", "من → إلى", "ملاحظة"]
    ]

    for log in logs:
        table_data.append([
            log.created_at.strftime("%Y-%m-%d %H:%M"),
            log.user.name if log.user else "النظام",
            log.action,
            f"{log.old_status} → {log.new_status}",
            log.note or ""
        ])

    table = Table(table_data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.lightgrey),
        ("GRID", (0,0), (-1,-1), 1, colors.black),
        ("ALIGN", (0,0), (-1,-1), "CENTER"),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
    ]))

    elements.append(table)

    doc.build(elements)

    buffer.seek(0)
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"request_{req.id}_report.pdf",
        mimetype="application/pdf"
    )


if __name__ == "__main__":
    app.run(debug=True)
