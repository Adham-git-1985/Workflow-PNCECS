from models import Role, RolePermission, User
from flask import (
    render_template, Blueprint,
    request, redirect, url_for, flash
)
from flask_login import login_required
from utils.perms import perm_required
from permissions import roles_required, role_perm_required
from models import WorkflowRequest, SystemSetting, AuditLog, ArchivedFile, WorkflowRoutingRule, RequestType, Organization, Directorate, Department, WorkflowTemplate
from extensions import db
from sqlalchemy import func, or_
from datetime import datetime, timedelta
from filters.request_filters import apply_request_filters
from filters.request_filters import get_sla_days, get_escalation_days
from sqlalchemy import case

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

DASHBOARD_CACHE = {
    "data": None,
    "last_update": None
}

DASHBOARD_TTL_SECONDS = 30


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
@role_perm_required("VIEW_DASHBOARD")
def dashboard():
    now = datetime.utcnow()

    if (
        DASHBOARD_CACHE["data"]
        and DASHBOARD_CACHE["last_update"]
        and (now - DASHBOARD_CACHE["last_update"]).seconds < DASHBOARD_TTL_SECONDS
    ):
        return render_template(
            "admin/dashboard.html",
            **DASHBOARD_CACHE["data"]
        )

    stats = db.session.query(
        func.count(WorkflowRequest.id),
        func.sum(case((WorkflowRequest.status == "APPROVED", 1), else_=0)),
        func.sum(case((WorkflowRequest.status == "REJECTED", 1), else_=0)),
        func.sum(case((WorkflowRequest.status == "DRAFT", 1), else_=0)),
        func.sum(
            case(
                (WorkflowRequest.status.notin_(FINAL_STATUSES + ["DRAFT"]), 1),
                else_=0
            )
        )
    ).one()

    total, approved, rejected, drafts, in_progress = stats
    delegated = 0

    SLA_DAYS = get_sla_days()
    sla_threshold = now - timedelta(days=SLA_DAYS)

    aging_requests = (
        WorkflowRequest.query
        .filter(
            WorkflowRequest.status.notin_(FINAL_STATUSES),
            WorkflowRequest.created_at <= sla_threshold
        )
        .order_by(WorkflowRequest.created_at.asc())
        .limit(10)
        .all()
    )

    archive_total = ArchivedFile.query.count()
    archive_active = ArchivedFile.query.filter_by(is_deleted=False).count()
    archive_deleted = ArchivedFile.query.filter_by(is_deleted=True).count()

    context = {
        "counters": {
            "total": total,
            "approved": approved,
            "rejected": rejected,
            "drafts": drafts,
            "in_progress": in_progress,
            "delegated": delegated
        },
        "archive_counters": {
            "total": archive_total,
            "active": archive_active,
            "deleted": archive_deleted
        },
        "aging_requests": aging_requests,
        "sla_days": SLA_DAYS,
        "now": now
    }

    DASHBOARD_CACHE["data"] = context
    DASHBOARD_CACHE["last_update"] = now

    return render_template("admin/dashboard.html", **context)



@admin_bp.route("/permissions", methods=["GET", "POST"])
@login_required
@roles_required("ADMIN")  # SUPER_ADMIN bypass exists in roles_required
def manage_permissions():
    """Manage permissions by ROLE (RolePermission table).

    Note: This is different from per-user CRUD permissions in masterdata/permissions.
    """
    # Prefer roles from master data table
    roles = Role.query.filter_by(is_active=True).order_by(Role.code.asc()).all()
    role_codes = [r.code for r in roles]

    # If roles table empty for some reason, fall back to roles in DB users
    if not role_codes:
        role_codes = [
            r for (r,) in db.session.query(User.role).distinct().order_by(User.role.asc()).all()
            if (r or "").strip()
        ]

    permissions = [
        "VIEW_DASHBOARD",
        "VIEW_ESCALATIONS",
        "CREATE_REQUEST",
        "APPROVE_REQUEST",
        "UPLOAD_ATTACHMENT",
        "SIGN_ARCHIVE",
        "DELETE_ARCHIVE",
        "VIEW_TIMELINE",
    ]

    selected_role = (request.args.get("role") or "").strip()
    if request.method == "POST":
        selected_role = (request.form.get("role") or "").strip()
        perms = request.form.getlist("permissions")

        if not selected_role:
            flash("اختر Role.", "danger")
            return redirect(url_for("admin.manage_permissions"))

        RolePermission.query.filter_by(role=selected_role).delete(synchronize_session=False)

        for p in perms:
            p = (p or "").strip()
            if p:
                db.session.add(RolePermission(role=selected_role, permission=p))

        db.session.commit()
        flash("تم تحديث صلاحيات الدور.", "success")
        return redirect(url_for("admin.manage_permissions", role=selected_role))

    # Pre-check existing permissions for selected role
    checked = set()
    if selected_role:
        rows = RolePermission.query.filter_by(role=selected_role).all()
        checked = { (r.permission or "").strip() for r in rows if r.permission }

    return render_template(
        "admin/permissions.html",
        role_codes=role_codes,
        permissions=permissions,
        selected_role=selected_role,
        checked=checked,
    )

@admin_bp.route("/requests")
@login_required
@roles_required("ADMIN")
def admin_requests():

    base_query = WorkflowRequest.query

    query = apply_request_filters(
        base_query,
        request.args
    )

    requests = query.order_by(
        WorkflowRequest.created_at.desc()
    ).all()

    return render_template(
        "admin/requests.html",
        requests=requests,
        is_admin=True
    )

@admin_bp.route("/escalations")
@login_required
@role_perm_required("VIEW_ESCALATIONS")
def escalations():

    now = datetime.utcnow()
    esc_deadline = now - timedelta(
        days=get_sla_days() + get_escalation_days()
    )

    escalated_requests = WorkflowRequest.query.filter(
        WorkflowRequest.status.notin_(["APPROVED", "REJECTED"]),
        WorkflowRequest.created_at < esc_deadline
    ).order_by(
        WorkflowRequest.created_at.asc()
    ).all()

    return render_template(
        "admin/escalations.html",
        requests=escalated_requests
    )

# =========================
# Workflow Routing Rules (Admin)
# =========================
@admin_bp.route("/workflow-routing")
@login_required
@perm_required("WORKFLOW_ROUTING_READ")
def workflow_routing_list():
    q = (request.args.get("q") or "").strip()

    query = (
        WorkflowRoutingRule.query
        .outerjoin(RequestType, WorkflowRoutingRule.request_type_id == RequestType.id)
        .outerjoin(WorkflowTemplate, WorkflowRoutingRule.template_id == WorkflowTemplate.id)
        .outerjoin(Organization, WorkflowRoutingRule.organization_id == Organization.id)
        .outerjoin(Directorate, WorkflowRoutingRule.directorate_id == Directorate.id)
        .outerjoin(Department, WorkflowRoutingRule.department_id == Department.id)
    )

    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            RequestType.code.ilike(like),
            RequestType.name_ar.ilike(like),
            RequestType.name_en.ilike(like),
            WorkflowTemplate.name.ilike(like),
            Organization.name_ar.ilike(like),
            Organization.name_en.ilike(like),
            Directorate.name_ar.ilike(like),
            Directorate.name_en.ilike(like),
            Department.name_ar.ilike(like),
            Department.name_en.ilike(like),
        ))

    rules = (
        query
        .order_by(
            WorkflowRoutingRule.is_active.desc(),
            WorkflowRoutingRule.priority.asc(),
            WorkflowRoutingRule.id.desc()
        )
        .all()
    )
    return render_template("admin/workflow_routing/list.html", rules=rules, q=q)


@admin_bp.route("/workflow-routing/new", methods=["GET", "POST"])
@login_required
@perm_required("WORKFLOW_ROUTING_CREATE")
def workflow_routing_new():
    r = WorkflowRoutingRule()
    return _workflow_routing_form(r, is_new=True)


@admin_bp.route("/workflow-routing/<int:rule_id>/edit", methods=["GET", "POST"])
@login_required
@perm_required("WORKFLOW_ROUTING_UPDATE")
def workflow_routing_edit(rule_id):
    r = WorkflowRoutingRule.query.get_or_404(rule_id)
    return _workflow_routing_form(r, is_new=False)


def _workflow_routing_form(r: WorkflowRoutingRule, is_new: bool):
    request_types = RequestType.query.order_by(RequestType.name_ar.asc()).all()
    orgs = Organization.query.order_by(Organization.name_ar.asc()).all()
    dirs = Directorate.query.order_by(Directorate.name_ar.asc()).all()
    depts = Department.query.order_by(Department.name_ar.asc()).all()
    templates = WorkflowTemplate.query.order_by(WorkflowTemplate.name.asc()).all()

    if request.method == "POST":
        # required
        rt_id = (request.form.get("request_type_id") or "").strip()
        template_id = (request.form.get("template_id") or "").strip()

        if not rt_id.isdigit():
            flash("اختر نوع طلب.", "danger")
            return redirect(request.url)
        if not template_id.isdigit():
            flash("اختر مسار (Template).", "danger")
            return redirect(request.url)

        r.request_type_id = int(rt_id)
        r.template_id = int(template_id)

        # optional hierarchy
        org_id = (request.form.get("organization_id") or "").strip()
        dir_id = (request.form.get("directorate_id") or "").strip()
        dept_id = (request.form.get("department_id") or "").strip()

        r.organization_id = int(org_id) if org_id.isdigit() else None
        r.directorate_id = int(dir_id) if dir_id.isdigit() else None
        r.department_id = int(dept_id) if dept_id.isdigit() else None

        # priority + active
        try:
            r.priority = int((request.form.get("priority") or "100").strip())
        except Exception:
            r.priority = 100

        r.is_active = (request.form.get("is_active") == "1")

        # validation: don't allow dept without dir, or dir without org
        if r.department_id and not r.directorate_id:
            flash("لا يمكن تحديد دائرة بدون تحديد إدارة.", "danger")
            return redirect(request.url)
        if r.directorate_id and not r.organization_id:
            flash("لا يمكن تحديد إدارة بدون تحديد منظمة.", "danger")
            return redirect(request.url)

        if is_new:
            db.session.add(r)

        db.session.commit()
        flash("تم حفظ قاعدة التوجيه.", "success")
        return redirect(url_for("admin.workflow_routing_list"))

    return render_template(
        "admin/workflow_routing/form.html",
        r=r,
        is_new=is_new,
        request_types=request_types,
        orgs=orgs,
        dirs=dirs,
        depts=depts,
        templates=templates
    )


@admin_bp.route("/workflow-routing/<int:rule_id>/delete", methods=["POST"])
@login_required
@perm_required("WORKFLOW_ROUTING_DELETE")
def workflow_routing_delete(rule_id):
    r = WorkflowRoutingRule.query.get_or_404(rule_id)
    db.session.delete(r)
    db.session.commit()
    flash("تم حذف قاعدة التوجيه.", "warning")
    return redirect(url_for("admin.workflow_routing_list"))
