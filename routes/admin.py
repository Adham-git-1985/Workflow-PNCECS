from flask import (
    render_template, Blueprint,
    request, redirect, url_for, flash
)
from flask_login import login_required
from permissions import roles_required
from models import WorkflowRequest, SystemSetting, AuditLog
from extensions import db
from sqlalchemy import func
from datetime import datetime, timedelta


# =========================
# Blueprint
# =========================
admin_bp = Blueprint(
    "admin",
    __name__,
    url_prefix="/admin"
)

# =========================
# Constants
# =========================
FINAL_STATUSES = ["APPROVED", "REJECTED"]

ESCALATION_ROLE_MAP = {
    "dept_head": "secretary_general",
    "finance": "secretary_general"
}

SYSTEM_USER_ID = None  # system action


# =========================
# Helpers
# =========================
def get_sla_days():
    setting = SystemSetting.query.filter_by(key="SLA_DAYS").first()
    return int(setting.value) if setting else 3


def get_escalation_days():
    setting = SystemSetting.query.filter_by(key="ESCALATION_DAYS").first()
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

    setting = SystemSetting.query.filter_by(key="SLA_DAYS").first()

    if not setting:
        setting = SystemSetting(
            key="SLA_DAYS",
            value=str(sla_days)
        )
        db.session.add(setting)
    else:
        setting.value = str(sla_days)

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

    from flask import current_app
    print("ROUTE APP ID:", id(current_app._get_current_object()))

    # ===== Counters =====
    total = db.session.query(func.count(WorkflowRequest.id)).scalar()

    approved = WorkflowRequest.query.filter(
        WorkflowRequest.status == "APPROVED"
    ).count()

    rejected = WorkflowRequest.query.filter(
        WorkflowRequest.status == "REJECTED"
    ).count()

    drafts = WorkflowRequest.query.filter(
        WorkflowRequest.status == "DRAFT"
    ).count()

    in_progress = WorkflowRequest.query.filter(
        WorkflowRequest.status.notin_(FINAL_STATUSES + ["DRAFT"])
    ).count()

    delegated = 0  # TODO: implement delegation counter

    # ===== SLA / Escalation =====
    SLA_DAYS = get_sla_days()
    ESCALATION_DAYS = get_escalation_days()

    now = datetime.utcnow()

    sla_threshold = now - timedelta(days=SLA_DAYS)
    escalation_threshold = now - timedelta(
        days=SLA_DAYS + ESCALATION_DAYS
    )

    # ===== Aging Requests (for display) =====
    aging_requests = (
        WorkflowRequest.query
        .filter(
            WorkflowRequest.status.notin_(FINAL_STATUSES),
            WorkflowRequest.created_at <= sla_threshold
        )
        .order_by(WorkflowRequest.created_at.asc())
        .all()
    )

    # ===== Escalation Logic =====
    escalated_requests = (
        WorkflowRequest.query
        .filter(
            WorkflowRequest.status.notin_(FINAL_STATUSES),
            WorkflowRequest.created_at <= escalation_threshold,
            WorkflowRequest.is_escalated == False
        )
        .all()
    )

    for req in escalated_requests:
        old_status = req.status
        old_role = req.current_role or "dept_head"

        # Update request
        req.is_escalated = True
        req.escalated_at = now
        req.status = "ESCALATED"

        new_role = ESCALATION_ROLE_MAP.get(
            old_role,
            "secretary_general"
        )
        req.current_role = new_role

        # Audit log
        log = AuditLog(
            request_id=req.id,
            user_id=SYSTEM_USER_ID,
            action="ESCALATION",
            old_status=old_status,
            new_status="ESCALATED",
            note=(
                f"Escalated from {old_role} to {new_role} "
                f"after {SLA_DAYS + ESCALATION_DAYS} days"
            )
        )
        db.session.add(log)

    if escalated_requests:
        db.session.commit()

    # ===== Render =====
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
        now=now
    )

