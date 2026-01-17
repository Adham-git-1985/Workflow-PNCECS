from flask import (
    render_template, Blueprint,
    request, redirect, url_for, flash
)
from flask_login import login_required
from permissions import roles_required
from models import WorkflowRequest, SystemSetting
from extensions import db
from sqlalchemy import func
from datetime import datetime, timedelta

admin_bp = Blueprint(
    "admin",
    __name__,
    url_prefix="/admin"
)

# =========================
# Helpers
# =========================
def get_sla_days():
    sla_setting = SystemSetting.query.filter_by(key="SLA_DAYS").first()
    return int(sla_setting.value) if sla_setting else 3

# =========================
# Escalation
# =========================
def get_escalation_days():
    setting = SystemSetting.query.filter_by(
        key="ESCALATION_DAYS"
    ).first()
    return int(setting.value) if setting else 2

# =========================
# Update SLA
# =========================
@admin_bp.route("/update-sla", methods=["POST"])
@login_required
@roles_required("ADMIN")
def update_sla():

    sla_days = request.form.get("sla_days", type=int)

    if sla_days is None or sla_days <= 0:
        flash("Invalid SLA value", "danger")
        return redirect(url_for("admin.dashboard"))

    sla_setting = SystemSetting.query.filter_by(key="SLA_DAYS").first()

    if not sla_setting:
        sla_setting = SystemSetting(key="SLA_DAYS", value=str(sla_days))
        db.session.add(sla_setting)
    else:
        sla_setting.value = str(sla_days)

    db.session.commit()

    flash(f"SLA updated to {sla_days} days", "success")
    return redirect(url_for("admin.dashboard"))

# =========================
# Admin Dashboard
# =========================
@admin_bp.route("/dashboard")
@login_required
@roles_required("ADMIN")
def dashboard():

    total = db.session.query(func.count(WorkflowRequest.id)).scalar()

    approved = db.session.query(func.count(WorkflowRequest.id)) \
        .filter(WorkflowRequest.status == "APPROVED") \
        .scalar()

    rejected = db.session.query(func.count(WorkflowRequest.id)) \
        .filter(WorkflowRequest.status == "REJECTED") \
        .scalar()

    drafts = db.session.query(func.count(WorkflowRequest.id)) \
        .filter(WorkflowRequest.status == "DRAFT") \
        .scalar()

    in_progress = db.session.query(func.count(WorkflowRequest.id)) \
        .filter(
            WorkflowRequest.status.notin_(
                ["APPROVED", "REJECTED", "DRAFT"]
            )
        ) \
        .scalar()

    delegated = 0  # مؤقتًا

    # ===== SLA / Aging =====
    SLA_DAYS = get_sla_days()
    sla_threshold = datetime.utcnow() - timedelta(days=SLA_DAYS)

    aging_requests = (
        WorkflowRequest.query
        .filter(
            WorkflowRequest.status.notin_(["APPROVED", "REJECTED"]),
            WorkflowRequest.created_at <= sla_threshold
        )
        .order_by(WorkflowRequest.created_at.asc())
        .all()
    )

    ESCALATION_DAYS = get_escalation_days()
    escalation_threshold = datetime.utcnow() - timedelta(
        days=(SLA_DAYS + ESCALATION_DAYS)
    )

    escalated_requests = (
        WorkflowRequest.query
            .filter(
            WorkflowRequest.status.notin_(["APPROVED", "REJECTED"]),
            WorkflowRequest.created_at <= escalation_threshold,
            WorkflowRequest.is_escalated == False
        )
            .all()
    )

    # Mark as escalated
    for req in escalated_requests:
        req.is_escalated = True

    if escalated_requests:
        db.session.commit()


    return render_template(
        "admin/dashboard.html",
        counters={
            "total": total,
            "approved": approved,
            "rejected": rejected,
            "drafts": drafts,
            "in_progress": in_progress,
            "delegated": delegated
        },
        aging_requests=aging_requests,
        sla_days=SLA_DAYS,
        now=datetime.utcnow()
    )
