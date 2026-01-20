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
from datetime import datetime, timedelta
import io
import os
from flask import session
import logging


from utils.events import emit_event



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
# Blueprints
from workflow.routes import workflow_bp
from admin.routes import admin_bp
from archive.routes import archive_bp
from audit.routes import audit_bp
from users.routes import users_bp


from filters.request_filters import apply_request_filters
from utils.permissions import get_effective_user
from filters.request_filters import get_sla_state
from services.escalation_service import run_escalation_if_needed

from filters.request_filters import get_sla_days, get_escalation_days

# ======================
# logging
# ======================

import logging
from logging.handlers import RotatingFileHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)

# =========================
# Logging (Workflow)
# =========================
if not os.path.exists("logs"):
    os.mkdir("logs")

file_handler = RotatingFileHandler(
    "logs/workflow.log",
    maxBytes=1_000_000,   # 1MB
    backupCount=5
)

file_handler.setLevel(logging.INFO)

formatter = logging.Formatter(
    "%(asctime)s | %(levelname)s | %(message)s"
)

file_handler.setFormatter(formatter)

logging.getLogger().setLevel(logging.INFO)
logging.getLogger().addHandler(file_handler)


ESCALATION_BADGE_CACHE = {
    "value": None,
    "last_update": None,
    "user_id": None
}

ESCALATION_BADGE_TTL = 30  # seconds

# ======================
# App Init
# ======================

app = Flask(__name__)
app.jinja_env.globals["get_sla_state"] = get_sla_state

# تأكيد وجود instance
os.makedirs(app.instance_path, exist_ok=True)

# المسار المطلق لقاعدة البيانات
db_path = os.path.join(app.instance_path, "workflow.db")

app.config["SECRET_KEY"] = "workflow-very-secret-key-2026"
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config.from_object("config.DevConfig")




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
app.register_blueprint(archive_bp)
app.register_blueprint(workflow_bp)



# ======================
# Login Manager
# ======================
@login_manager.user_loader
def load_user(user_id):
    logger.info(f"user_loader called with user_id={user_id}")

    user = User.query.get(int(user_id))

    if not user:
        logger.error("user_loader returned None ❌")
    else:
        logger.info(f"user_loader loaded user {user.email}")

    return user


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


@app.route("/")
@login_required
def index():
    logger.info(
        f"Accessing index | is_authenticated={current_user.is_authenticated} | user={current_user.get_id()}"
    )
    return redirect(url_for("my_requests"))


@app.before_request
def log_session():
    logger.debug(f"Session content: {dict(session)}")

@login_manager.unauthorized_handler
def unauthorized():
    logger.warning(
        f"Unauthorized access | path={request.path} | user={current_user.get_id()}"
    )
    return redirect(url_for("login"))


# ======================
# Auth Routes
# ======================
@app.route("/login", methods=["GET", "POST"])
def login():

    logger.info("Login page accessed")

    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")

        logger.info(f"Login attempt for email={email}")

        user = User.query.filter_by(email=email).first()

        if not user:
            logger.warning("Login failed: user not found")
            flash("Invalid credentials")
            return redirect(url_for("login"))

        if not user.check_password(password):
            logger.warning("Login failed: wrong password")
            flash("Invalid credentials")
            return redirect(url_for("login"))

        login_user(user)
        logger.info(
            f"Login success | user_id={user.id} | authenticated={current_user.is_authenticated}"
        )

        return redirect(url_for("index"))

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

        #  Audit (موجود عندك)
        log_action(
            request_obj=new_request,
            user=current_user,
            action="CREATE_REQUEST",
            old_status=None,
            new_status=status,
            note="تم إنشاء الطلب"
        )

        #  Notification + Audit مركزي
        emit_event(
            actor_id=current_user.id,
            action="REQUEST_CREATED",
            message=f"تم إنشاء طلب جديد رقم #{new_request.title}",
            target_type="WorkflowRequest",
            target_id=new_request.id,
            notify_role="ADMIN"
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
    run_escalation_if_needed()
    effective_user = get_effective_user()


    # 1️⃣ Base query (delegation-aware)
    base_query = WorkflowRequest.query.filter(
        WorkflowRequest.current_role == effective_user.role
    )

    # 2️⃣ Apply advanced filters
    filtered_query = apply_request_filters(
        base_query,
        request.args
    )

    # 3️⃣ Counters (من نفس filtered_query)
    counters = {
        "total": filtered_query.count(),
        "approved": filtered_query.filter(
            WorkflowRequest.status == "APPROVED"
        ).count(),
        "rejected": filtered_query.filter(
            WorkflowRequest.status == "REJECTED"
        ).count(),
        "in_progress": filtered_query.filter(
            WorkflowRequest.status.notin_(["APPROVED", "REJECTED"])
        ).count(),
    }

    sla_days = get_sla_days()
    esc_days = get_escalation_days()

    now = datetime.utcnow()
    sla_deadline = now - timedelta(days=sla_days)
    esc_deadline = now - timedelta(days=sla_days + esc_days)

    sla_counters = {
        "on_track": filtered_query.filter(
            WorkflowRequest.status.notin_(["APPROVED", "REJECTED"]),
            WorkflowRequest.created_at >= sla_deadline
        ).count(),

        "breached": filtered_query.filter(
            WorkflowRequest.status.notin_(["APPROVED", "REJECTED"]),
            WorkflowRequest.created_at < sla_deadline,
            WorkflowRequest.created_at >= esc_deadline
        ).count(),

        "escalated": filtered_query.filter(
            WorkflowRequest.status.notin_(["APPROVED", "REJECTED"]),
            WorkflowRequest.created_at < esc_deadline
        ).count(),
    }

    now = datetime.utcnow()
    esc_deadline = now - timedelta(
        days=get_sla_days() + get_escalation_days()
    )


    if (
            ESCALATION_BADGE_CACHE["value"] is not None
            and ESCALATION_BADGE_CACHE["last_update"]
            and ESCALATION_BADGE_CACHE["user_id"] == effective_user.id
            and (now - ESCALATION_BADGE_CACHE["last_update"]).seconds < ESCALATION_BADGE_TTL
    ):
        escalation_alerts_count = ESCALATION_BADGE_CACHE["value"]
    else:
        escalation_alerts_count = WorkflowRequest.query.filter(
            WorkflowRequest.current_role == effective_user.id,
            WorkflowRequest.status.notin_(["APPROVED", "REJECTED"]),
            WorkflowRequest.created_at < esc_deadline
        ).count()

        ESCALATION_BADGE_CACHE["value"] = escalation_alerts_count
        ESCALATION_BADGE_CACHE["last_update"] = now
        ESCALATION_BADGE_CACHE["user_id"] = effective_user.id

    # 4️⃣ Final list
    requests = filtered_query.order_by(
        WorkflowRequest.created_at.desc()
    ).all()

    return render_template(
        "inbox.html",
        requests=requests,
        counters=counters,
        sla_counters=sla_counters,
        escalation_alerts_count=escalation_alerts_count,
        is_admin=False,
        get_sla_state=get_sla_state
    )


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
        notif_type = "INFO"
    else:
        req.status = REJECT_STATUS
        req.current_role = None
        action_name = "REJECT"
        notif_type = "CRITICAL"

    log_action(
        request_obj=req,
        user=current_user,
        action=action_name,
        old_status=old_status,
        new_status=req.status,
        note=note
    )

    # 🔔 Notification + Audit
    emit_event(
        actor_id=current_user.id,
        action=f"REQUEST_{action_name}",
        message=(
            f"تم {'الموافقة على' if action == 'approve' else 'رفض'} "
            f"الطلب رقم #{req.id}"
        ),
        target_type="WorkflowRequest",
        target_id=req.id,
        notify_user_id=req.requester_id,  # صاحب الطلب
        notif_type=notif_type
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



if __name__ == "__main__":
    app.run(debug=True, use_reloader=False)



