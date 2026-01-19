from flask import (
    Flask, render_template, request,
    redirect, url_for, flash, send_file
)
from flask_login import (
    login_user, login_required,
    current_user, logout_user
)
from werkzeug.security import check_password_hash
from urllib.parse import urlparse
from flask_migrate import Migrate
from sqlalchemy import func
from datetime import datetime
import io
import os

# ======================
# Extensions
# ======================
from extensions import db, login_manager

# ======================
# Models
# ======================
from models import (
    User, WorkflowRequest,
    Approval, AuditLog
)

# ======================
# Blueprints
# ======================
from routes import admin_bp, audit_bp, users_bp

# ======================
# App Init
# ======================

app = Flask(__name__)

# تأكيد وجود instance
os.makedirs(app.instance_path, exist_ok=True)

# المسار المطلق لقاعدة البيانات
db_path = os.path.join(app.instance_path, "workflow.db")

app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key")
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False


# ======================
# Extensions Init
# ======================
db.init_app(app)
login_manager.init_app(app)
login_manager.login_view = "login"
migrate = Migrate(app, db)

# ======================
# Register Blueprints
# ======================
app.register_blueprint(admin_bp)
app.register_blueprint(audit_bp)
app.register_blueprint(users_bp)

# ======================
# Login Manager
# ======================
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ======================
# Workflow Constants
# ======================
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
FINAL_STATUSES = ["APPROVED", "REJECTED"]

# ======================
# Auth Routes
# ======================
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        user = User.query.filter_by(email=email).first()

        if user and check_password_hash(user.password_hash, password):
            login_user(user, remember=True)

            next_page = request.args.get("next")
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

# ======================
# Create Request
# ======================
@app.route("/request/new", methods=["GET", "POST"])
@login_required
def create_request():
    if request.method == "POST":
        title = request.form.get("title")
        description = request.form.get("description")
        action = request.form.get("action")

        status = "DRAFT" if action == "save" else "SUBMITTED"
        current_role = STATUS_ROLE_MAP.get(status)

        new_request = WorkflowRequest(
            title=title,
            description=description,
            status=status,
            requester_id=current_user.id,
            current_role=current_role
        )

        db.session.add(new_request)
        db.session.flush()

        log_action(
            request_obj=new_request,
            user=current_user,
            action="CREATE_REQUEST",
            old_status=None,
            new_status=status,
            note="تم إنشاء الطلب"
        )

        db.session.commit()
        flash("تم حفظ الطلب بنجاح", "success")
        return redirect(url_for("create_request"))

    return render_template("create_request.html")

# ======================
# My Requests
# ======================
@app.route("/my-requests")
@login_required
def my_requests():
    status = request.args.get("status")

    base_query = WorkflowRequest.query.filter_by(
        requester_id=current_user.id
    )

    counters = {
        "total": base_query.count(),
        "approved": base_query.filter_by(status="APPROVED").count(),
        "rejected": base_query.filter_by(status="REJECTED").count(),
        "draft": base_query.filter_by(status="DRAFT").count(),
        "in_progress": base_query.filter(
            WorkflowRequest.status.notin_(
                FINAL_STATUSES + ["DRAFT"]
            )
        ).count()
    }

    if status:
        if status == "in_progress":
            base_query = base_query.filter(
                WorkflowRequest.status.notin_(
                    FINAL_STATUSES + ["DRAFT"]
                )
            )
        else:
            base_query = base_query.filter(
                WorkflowRequest.status == status.upper()
            )

    requests = base_query.order_by(
        WorkflowRequest.id.desc()
    ).all()

    return render_template(
        "my_requests.html",
        requests=requests,
        counters=counters,
        last_update=datetime.utcnow()
    )

# ======================
# Inbox
# ======================
@app.route("/inbox")
@login_required
def inbox():
    requests = WorkflowRequest.query.filter(
        WorkflowRequest.current_role == current_user.role,
        WorkflowRequest.status.notin_(FINAL_STATUSES)
    ).all()

    return render_template("inbox.html", requests=requests)

# ======================
# Review / Actions
# ======================
@app.route("/request/<int:request_id>")
@login_required
def review_request(request_id):
    req = WorkflowRequest.query.get_or_404(request_id)

    if req.current_role != current_user.role:
        flash("غير مصرح لك بمراجعة هذا الطلب", "danger")
        return redirect(url_for("inbox"))

    return render_template("review_request.html", req=req)


@app.route("/request/<int:request_id>/action", methods=["POST"])
@login_required
def request_action(request_id):
    req = WorkflowRequest.query.get_or_404(request_id)

    if req.current_role != current_user.role:
        flash("غير مصرح لك بتنفيذ هذا الإجراء", "danger")
        return redirect(url_for("inbox"))

    action = request.form.get("action")
    note = request.form.get("note")
    old_status = req.status
    old_role = req.current_role

    if action == "approve":
        req.status = NEXT_STATUS_MAP.get(req.status, req.status)
        req.current_role = STATUS_ROLE_MAP.get(req.status)
        action_name = "APPROVE"
    else:
        req.status = REJECT_STATUS
        req.current_role = None
        action_name = "REJECT"

    log_action(
        request_obj=req,
        user=current_user,
        action=action_name,
        old_status=old_status,
        new_status=req.status,
        note=note
    )

    db.session.add(Approval(
        request_id=req.id,
        user_id=current_user.id,
        action=action,
        note=note
    ))

    db.session.commit()
    flash("تم تسجيل الإجراء بنجاح", "success")
    return redirect(url_for("inbox"))

# ======================
# Audit / PDF
# ======================
def log_action(request_obj, user, action, old_status, new_status, note=None):
    db.session.add(AuditLog(
        request_id=request_obj.id,
        user_id=user.id if user else None,
        action=action,
        old_status=old_status,
        new_status=new_status,
        note=note
    ))


@app.route("/request/<int:request_id>/audit")
@login_required
def request_audit(request_id):
    logs = AuditLog.query.filter_by(
        request_id=request_id
    ).order_by(AuditLog.created_at.asc()).all()

    req = WorkflowRequest.query.get_or_404(request_id)
    return render_template("audit_log.html", logs=logs, req=req)


@app.route("/request/<int:request_id>/pdf")
@login_required
def request_pdf(request_id):
    req = WorkflowRequest.query.get_or_404(request_id)
    logs = AuditLog.query.filter_by(request_id=request_id).all()

    buffer = io.BytesIO()
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph,
        Spacer, Table
    )
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.pagesizes import A4

    doc = SimpleDocTemplate(buffer, pagesize=A4)
    styles = getSampleStyleSheet()
    elements = [
        Paragraph("<b>تقرير مسار الطلب</b>", styles["Title"]),
        Spacer(1, 12)
    ]

    table_data = [["التاريخ", "الإجراء", "من → إلى", "ملاحظة"]]
    for log in logs:
        table_data.append([
            log.created_at.strftime("%Y-%m-%d %H:%M"),
            log.action,
            f"{log.old_status} → {log.new_status}",
            log.note or ""
        ])

    elements.append(Table(table_data))
    doc.build(elements)

    buffer.seek(0)
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"request_{req.id}.pdf",
        mimetype="application/pdf"
    )


if __name__ == "__main__":
    app.run(debug=True, use_reloader=False)
