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


app = Flask(__name__)
app.secret_key = "secret"

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

db.init_app(app)



@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")

        user = User.query.filter_by(email=email).first()

        if not user or not user.check_password(password):
            flash("بيانات الدخول غير صحيحة", "danger")
            return redirect(url_for("login"))

        session["user_id"] = user.id
        session["role"] = user.role

        return redirect(url_for("inbox"))

    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return User.query.get(user_id)

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper

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
            user=get_current_user(),
            action="CREATE_REQUEST",
            old_status=old_status,
            new_status=new_status,
            note="تم إنشاء الطلب"
        )

        db.session.commit()

        flash("تم حفظ الطلب بنجاح")
        return redirect(url_for("create_request"))

    return render_template("create_request.html")

@app.route("/inbox")
@login_required
def inbox():
    current_user = get_current_user()

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
        user=current_user
    )


@app.route("/request/<int:request_id>")
def review_request(request_id):
    req = WorkflowRequest.query.get_or_404(request_id)
    return render_template("review_request.html", req=req)

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
        req=req
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
