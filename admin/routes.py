from models import Role, RolePermission, User
from flask import (
    render_template, Blueprint,
    request, redirect, url_for, flash,
    send_file,
    current_app
)
from flask_login import login_required, current_user, logout_user
from utils.perms import perm_required
from permissions import roles_required, role_perm_required
from models import WorkflowRequest, SystemSetting, AuditLog, ArchivedFile, WorkflowRoutingRule, RequestType, Organization, Directorate, Department, WorkflowTemplate, RequestEscalation, OrgNode, OrgNodeType
from extensions import db
from sqlalchemy import func, or_
from datetime import datetime, timedelta
from filters.request_filters import apply_request_filters
from filters.request_filters import get_sla_days, get_escalation_days
from sqlalchemy import case
from io import BytesIO

import os
import json
import shutil
import sqlite3
import zipfile
import tempfile

from utils.excel import make_xlsx_bytes, make_xlsx_bytes_multi
from utils.importer import read_excel_rows, pick, to_str, to_int, to_bool, replace_all
from utils.org_dynamic import build_org_node_picker_tree
from portal.perm_defs import ALL_KEYS as PORTAL_ALL_KEYS

# =========================
# Blueprint
# =========================
admin_bp = Blueprint(
    "admin",
    __name__,
    url_prefix="/admin"
)

# Register sub-modules
from .evaluations import register_evaluation_routes
register_evaluation_routes(admin_bp)

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


def get_trash_retention_days() -> int:
    """How many days deleted archive files remain in recycle bin before purge."""
    setting = SystemSetting.query.filter_by(key="TRASH_RETENTION_DAYS").first()
    try:
        return int(setting.value) if setting and setting.value is not None else 30
    except Exception:
        return 30


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
# Update Recycle Bin Retention
# =========================
@admin_bp.route("/update-trash-retention", methods=["POST"])
@login_required
@roles_required("ADMIN")
def update_trash_retention():
    days = request.form.get("trash_retention_days", type=int)
    if days is None or days < 1:
        flash("قيمة سياسة الاحتفاظ غير صحيحة", "danger")
        return redirect(url_for("admin.dashboard"))

    setting = SystemSetting.query.filter_by(key="TRASH_RETENTION_DAYS").first()
    if not setting:
        setting = SystemSetting(key="TRASH_RETENTION_DAYS", value=str(days))
        db.session.add(setting)
    else:
        setting.value = str(days)

    db.session.commit()
    flash(f"تم تحديث سياسة الاحتفاظ بسلة المحذوفات إلى {days} يوم", "success")
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
        "trash_retention_days": get_trash_retention_days(),
        "now": now
    }

    DASHBOARD_CACHE["data"] = context
    DASHBOARD_CACHE["last_update"] = now

    return render_template("admin/dashboard.html", **context)


@admin_bp.route("/dashboard/export.xlsx")
@login_required
@role_perm_required("VIEW_DASHBOARD")
def dashboard_export_excel():
    """Export dashboard counters + overdue list to Excel."""
    now = datetime.utcnow()
    SLA_DAYS = get_sla_days()
    sla_threshold = now - timedelta(days=SLA_DAYS)

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
    archive_total = ArchivedFile.query.count()
    archive_active = ArchivedFile.query.filter_by(is_deleted=False).count()
    archive_deleted = ArchivedFile.query.filter_by(is_deleted=True).count()

    overdue = (
        WorkflowRequest.query
        .filter(
            WorkflowRequest.status.notin_(FINAL_STATUSES),
            WorkflowRequest.created_at <= sla_threshold
        )
        .order_by(WorkflowRequest.created_at.asc())
        .all()
    )

    headers = [
        "Request ID", "Title", "Status", "Created At", "Days Open",
        "Escalated", "Current Role"
        "PORTAL_VIEW",
        "HR_ATTENDANCE_IMPORT",
        "CORR_VIEW",
        "CORR_IN_CREATE",
        "CORR_OUT_CREATE",
        "CORR_MANAGE",
    ]

    rows = []
    for r in overdue:
        rows.append([
            r.id,
            r.title,
            r.status,
            r.created_at.strftime("%Y-%m-%d %H:%M"),
            (now - r.created_at).days,
            "YES" if r.is_escalated else "NO",
            r.current_role,
        ])

    # Prepend summary rows (as plain rows)
    summary_headers = ["Metric", "Value"]
    summary_rows = [
        ("Total Requests", total),
        ("Approved", approved),
        ("Rejected", rejected),
        ("Drafts", drafts),
        ("In Progress", in_progress),
        ("SLA Days", SLA_DAYS),
        ("Archive Total", archive_total),
        ("Archive Active", archive_active),
        ("Archive Deleted", archive_deleted),
        ("Trash Retention Days", get_trash_retention_days()),
        ("Exported At", now.strftime("%Y-%m-%d %H:%M")),
    ]

    # Build workbook with two sheets
    try:
        # Create two sheets by generating bytes twice and merging is heavy;
        # Instead: build manually using openpyxl inside util function.
        import openpyxl
        from openpyxl.styles import Font, Alignment
        from openpyxl.utils import get_column_letter

        wb = openpyxl.Workbook()
        ws1 = wb.active
        ws1.title = "Summary"[:31]
        ws1.append(list(summary_headers))
        for c in range(1, 3):
            cell = ws1.cell(row=1, column=c)
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for m, v in summary_rows:
            ws1.append([m, v])
        ws1.freeze_panes = "A2"
        ws1.column_dimensions["A"].width = 28
        ws1.column_dimensions["B"].width = 20

        ws2 = wb.create_sheet("Overdue")
        ws2.append(headers)
        for c in range(1, len(headers) + 1):
            cell = ws2.cell(row=1, column=c)
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for r in rows:
            ws2.append(r)
        ws2.freeze_panes = "A2"
        for col_idx in range(1, len(headers) + 1):
            col_letter = get_column_letter(col_idx)
            ws2.column_dimensions[col_letter].width = 22

        bio = BytesIO()
        wb.save(bio)
        data = bio.getvalue()
    except Exception:
        # Fallback single-sheet export
        data = make_xlsx_bytes("Overdue", headers, rows)

    filename = f"admin_dashboard_{now.strftime('%Y%m%d_%H%M')}.xlsx"
    return send_file(
        BytesIO(data),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )



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
        "DELEGATION_MANAGE",
        "DELEGATION_SELF",
        # Portal/HR keys (so role-based access works from this UI)
        "PORTAL_READ",
        "HR_SYSTEM_EVALUATION_VIEW",
    ]

    selected_role = (request.args.get("role") or "").strip()
    if request.method == "POST":
        selected_role = (request.form.get("role") or "").strip()
        perms = request.form.getlist("permissions")

        if not selected_role:
            flash("اختر Role.", "danger")
            return redirect(url_for("admin.manage_permissions"))

        known = set(permissions) | set(PORTAL_ALL_KEYS)

        # احذف فقط الصلاحيات المعروفة لتجنب مسح صلاحيات أخرى قد تكون أضيفت لاحقًا
        RolePermission.query.filter_by(role=selected_role).filter(RolePermission.permission.in_(known)).delete(synchronize_session=False)

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


@admin_bp.route("/requests/export.xlsx")
@login_required
@roles_required("ADMIN")
def admin_requests_export_excel():
    """Export admin requests list (with the same advanced filters) to Excel."""
    base_query = WorkflowRequest.query
    query = apply_request_filters(base_query, request.args)

    reqs = query.order_by(WorkflowRequest.created_at.desc()).all()

    headers = [
        "ID",
        "Title",
        "Status",
        "Created At",
        "Requester",
        "Current Role",
        "Request Type",
    ]

    rows = []
    for r in reqs:
        rt_label = ""
        try:
            if getattr(r, "request_type", None):
                rt_label = (r.request_type.name_ar or r.request_type.code or "")
        except Exception:
            rt_label = ""

        rows.append([
            r.id,
            r.title,
            r.status,
            r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "",
            (r.requester.email if getattr(r, "requester", None) else ""),
            r.current_role,
            rt_label,
        ])

    data = make_xlsx_bytes("Requests", headers, rows)
    filename = f"admin_requests_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.xlsx"
    return send_file(
        BytesIO(data),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


@admin_bp.route("/escalations")
@login_required
@role_perm_required("VIEW_ESCALATIONS")
def escalations():

    now = datetime.utcnow()
    esc_deadline = now - timedelta(
        days=get_sla_days() + get_escalation_days()
    )

    # 1) SLA-overdue (legacy definition)
    escalated_requests = WorkflowRequest.query.filter(
        WorkflowRequest.status.notin_(["APPROVED", "REJECTED"]),
        WorkflowRequest.created_at < esc_deadline
    ).order_by(WorkflowRequest.created_at.asc()).all()

    # 2) Full escalation log (manual + system) stored in RequestEscalation
    manual_escalations = (
        RequestEscalation.query
        .order_by(RequestEscalation.created_at.desc(), RequestEscalation.id.desc())
        .limit(500)
        .all()
    )

    return render_template(
        "admin/escalations.html",
        requests=escalated_requests,
        escalations=manual_escalations,
    )


@admin_bp.route("/escalations/export.xlsx")
@login_required
@role_perm_required("VIEW_ESCALATIONS")
def escalations_export_excel():
    """Export escalations report to Excel.

    Includes two sheets:
      - Escalation_Log: all recorded escalations (manual + system) from RequestEscalation
      - SLA_Overdue: legacy SLA-overdue requests
    """
    now = datetime.utcnow()
    esc_deadline = now - timedelta(days=get_sla_days() + get_escalation_days())

    escalated_requests = WorkflowRequest.query.filter(
        WorkflowRequest.status.notin_(["APPROVED", "REJECTED"]),
        WorkflowRequest.created_at < esc_deadline
    ).order_by(WorkflowRequest.created_at.asc()).all()

    # Sheet 1: Escalation log
    limit = request.args.get("limit", type=int) or 10000
    escalation_log = (
        RequestEscalation.query
        .order_by(RequestEscalation.created_at.desc(), RequestEscalation.id.desc())
        .limit(limit)
        .all()
    )

    headers_log = [
        "Escalation ID",
        "Request ID",
        "Step",
        "Category",
        "Created At",
        "From",
        "To (primary)",
        "Targets",
        "Description",
    ]

    rows_log = []
    for e in escalation_log:
        rows_log.append([
            e.id,
            e.request_id,
            getattr(e, "step_order", "") or "",
            e.category,
            e.created_at.strftime("%Y-%m-%d %H:%M") if e.created_at else "",
            (e.from_user.email if getattr(e, "from_user", None) else ""),
            (e.to_user.email if getattr(e, "to_user", None) else ""),
            getattr(e, "targets", "") or "",
            (e.description or "")[:2000],
        ])

    # Sheet 2: SLA-overdue requests (legacy)
    headers_overdue = [
        "ID",
        "Title",
        "Created At",
        "Status",
        "Current Role",
        "Days Open",
    ]

    rows_overdue = []
    for r in escalated_requests:
        days_open = (now - r.created_at).days if r.created_at else ""
        rows_overdue.append([
            r.id,
            r.title,
            r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "",
            r.status,
            r.current_role,
            days_open,
        ])

    data = make_xlsx_bytes_multi([
        ("Escalation_Log", headers_log, rows_log),
        ("SLA_Overdue", headers_overdue, rows_overdue),
    ])

    filename = f"escalations_{now.strftime('%Y%m%d_%H%M')}.xlsx"
    return send_file(
        BytesIO(data),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
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
        .outerjoin(OrgNode, WorkflowRoutingRule.org_node_id == OrgNode.id)
        .outerjoin(OrgNodeType, OrgNode.type_id == OrgNodeType.id)
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
            OrgNode.name_ar.ilike(like),
            OrgNode.name_en.ilike(like),
            OrgNode.code.ilike(like),
            OrgNodeType.name_ar.ilike(like),
            OrgNodeType.name_en.ilike(like),
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


@admin_bp.route("/workflow-routing/org-node-tree")
@login_required
@perm_required("WORKFLOW_ROUTING_READ")
def workflow_routing_org_node_tree():
    """AJAX: return OrgNode picker tree for routing rules (mode=routes)."""
    mode = (request.args.get("mode") or "routes").strip().lower()
    sel = (request.args.get("selected") or "").strip()
    selected_id = int(sel) if sel.isdigit() else None
    tree = build_org_node_picker_tree(mode=mode)
    return render_template(
        "components/_org_node_picker_tree.html",
        tree=tree,
        selected_id=selected_id,
        mode=mode,
    )

@admin_bp.route("/workflow-routing/export.xlsx")
@login_required
@perm_required("WORKFLOW_ROUTING_READ")
def workflow_routing_export_excel():
    """Export workflow routing rules to .xlsx."""
    q = (request.args.get("q") or "").strip()

    query = (
        WorkflowRoutingRule.query
        .outerjoin(RequestType, WorkflowRoutingRule.request_type_id == RequestType.id)
        .outerjoin(WorkflowTemplate, WorkflowRoutingRule.template_id == WorkflowTemplate.id)
        .outerjoin(Organization, WorkflowRoutingRule.organization_id == Organization.id)
        .outerjoin(Directorate, WorkflowRoutingRule.directorate_id == Directorate.id)
        .outerjoin(Department, WorkflowRoutingRule.department_id == Department.id)
        .outerjoin(OrgNode, WorkflowRoutingRule.org_node_id == OrgNode.id)
        .outerjoin(OrgNodeType, OrgNode.type_id == OrgNodeType.id)
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
            OrgNode.name_ar.ilike(like),
            OrgNode.name_en.ilike(like),
            OrgNode.code.ilike(like),
            OrgNodeType.name_ar.ilike(like),
            OrgNodeType.name_en.ilike(like),
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

    headers = [
        "ID",
        "RequestType Code",
        "RequestType AR",
        "RequestType EN",
        "Organization",
        "Directorate",
        "Department",
        "OrgNode",
        "MatchSubtree",
        "Template",
        "Priority",
        "Active",
    ]

    rows = []
    for r in rules:
        rows.append([
            r.id,
            r.request_type.code if r.request_type else None,
            r.request_type.name_ar if r.request_type else None,
            r.request_type.name_en if r.request_type else None,
            r.organization.name_ar if r.organization else None,
            r.directorate.name_ar if r.directorate else None,
            r.department.name_ar if r.department else None,
            (f"{(r.org_node.type.name_ar if r.org_node and r.org_node.type else r.org_node.type.code if r.org_node and r.org_node.type else '')} — {r.org_node.name_ar}".strip(" —") if r.org_node else None),
            ("Yes" if getattr(r, "match_subtree", False) else "No") if r.org_node_id is not None else None,
            r.template.name if r.template else None,
            r.priority,
            "Yes" if r.is_active else "No",
        ])

    xlsx = make_xlsx_bytes("RoutingRules", headers, rows)
    bio = BytesIO(xlsx)
    bio.seek(0)

    filename = f"routing_rules_{datetime.utcnow().strftime('%Y-%m-%d_%H-%M')}.xlsx"
    return send_file(bio, as_attachment=True, download_name=filename)





@admin_bp.route("/workflow-routing/import-excel", methods=["POST"])
@login_required
@perm_required("WORKFLOW_ROUTING_UPDATE")
def workflow_routing_import_excel():
    """Import workflow routing rules from Excel.

    Modes:
      - safe: upsert by composite key (request_type + org + dir + dept)
      - replace: try delete-all then insert; if FK prevents deletion, soft-fallback by deactivating all then upsert

    Supported/tolerant columns:
      - request_type_code (or "RequestType Code") (required)
      - organization / organization_code (optional)
      - directorate / directorate_code (optional)
      - department / department_code (optional)
      - org_node / org_node_id / OrgNode (optional)
      - match_subtree / subtree / MatchSubtree (optional, default True when org_node provided)
      - template (name or id) (required)
      - priority (optional, default 100)
      - active / is_active (optional, default True)
    """
    mode = (request.form.get("mode") or "safe").strip().lower()
    file_storage = request.files.get("file")
    if not file_storage:
        flash("يرجى اختيار ملف Excel (.xlsx).", "danger")
        return redirect(url_for("admin.workflow_routing_list"))

    try:
        _title, rows, _headers = read_excel_rows(file_storage)
    except Exception as e:
        flash(f"تعذر قراءة ملف Excel: {e}", "danger")
        return redirect(url_for("admin.workflow_routing_list"))

    if not rows:
        flash("ملف Excel فارغ أو لا يحتوي صفوف بيانات.", "warning")
        return redirect(url_for("admin.workflow_routing_list"))

    def _clean_txt(v):
        s = to_str(v)
        return s.strip() if s else None

    def _resolve_obj(model, value, *, code_field: str = "code", name_fields=("name_ar", "name_en")):
        val = _clean_txt(value)
        if not val:
            return None
        # by id
        if val.isdigit():
            try:
                return model.query.get(int(val))
            except Exception:
                pass
        # by code
        if hasattr(model, code_field):
            col = getattr(model, code_field)
            obj = model.query.filter(col == val).first()
            if obj:
                return obj
            try:
                obj = model.query.filter(func.lower(col) == val.lower()).first()
                if obj:
                    return obj
            except Exception:
                pass
        # exact by names
        for nf in name_fields:
            if hasattr(model, nf):
                col = getattr(model, nf)
                obj = model.query.filter(col == val).first()
                if obj:
                    return obj
        # fallback ilike
        like = f"%{val}%"
        conds = []
        if hasattr(model, code_field):
            try:
                conds.append(getattr(model, code_field).ilike(like))
            except Exception:
                pass
        for nf in name_fields:
            if hasattr(model, nf):
                try:
                    conds.append(getattr(model, nf).ilike(like))
                except Exception:
                    pass
        if conds:
            obj = model.query.filter(or_(*conds)).first()
            if obj:
                return obj
        return None

    def _resolve_request_type(code_or_name):
        val = _clean_txt(code_or_name)
        if not val:
            return None
        code = val.upper()
        rt = RequestType.query.filter(RequestType.code == code).first()
        if rt:
            return rt
        # fallback by name
        rt = RequestType.query.filter(or_(RequestType.name_ar == val, RequestType.name_en == val)).first()
        if rt:
            return rt
        like = f"%{val}%"
        return RequestType.query.filter(or_(RequestType.code.ilike(like), RequestType.name_ar.ilike(like), RequestType.name_en.ilike(like))).first()

    def _resolve_template(name_or_id):
        val = _clean_txt(name_or_id)
        if not val:
            return None
        if val.isdigit():
            return WorkflowTemplate.query.get(int(val))
        # exact by name
        tpl = WorkflowTemplate.query.filter(WorkflowTemplate.name == val).first()
        if tpl:
            return tpl
        like = f"%{val}%"
        return WorkflowTemplate.query.filter(WorkflowTemplate.name.ilike(like)).first()

    def _parse_row(r):
        rt_val = pick(r, "request_type_code", "requesttypecode", "request type code", "RequestType Code", "rtype", "نوعالطلب", "نوع الطلب")
        org_val = pick(r, "organization", "org", "org_code", "organization_code", "organizationcode", "Organization", "منظمة")
        dir_val = pick(r, "directorate", "dir", "dir_code", "directorate_code", "directoratecode", "Directorate", "إدارة")
        dept_val = pick(r, "department", "dept", "dept_code", "department_code", "departmentcode", "Department", "دائرة")
        node_val = pick(r, "org_node", "orgnode", "org_node_id", "orgnodeid", "OrgNode", "Org Node", "node", "node_id", "عنصرهيكلي", "عنصر هيكلي", "عنصر")
        subtree_val = pick(r, "match_subtree", "subtree", "matchsubtree", "MatchSubtree", "Match Subtree", "شامل", "شامل الفروع")
        tpl_val = pick(r, "template", "template_id", "templateid", "Template", "workflow_template", "المسار")
        pr_val = pick(r, "priority", "Priority", "الأولوية")
        act_val = pick(r, "is_active", "active", "Active", "نشط", "فعال")

        rt = _resolve_request_type(rt_val)
        tpl = _resolve_template(tpl_val)
        org = _resolve_obj(Organization, org_val)
        direc = _resolve_obj(Directorate, dir_val)
        dept = _resolve_obj(Department, dept_val)
        node = _resolve_obj(OrgNode, node_val)

        match_subtree = to_bool(subtree_val, default=True)

        priority = to_int(pr_val, default=100) or 100
        is_active = to_bool(act_val, default=True)
        return rt, org, direc, dept, node, bool(match_subtree), tpl, int(priority), bool(is_active)

    skipped = 0
    created = 0
    updated = 0

    def _upsert_all():
        nonlocal created, updated, skipped
        created = updated = skipped = 0
        for rr in rows:
            rt, org, direc, dept, node, match_subtree, tpl, priority, is_active = _parse_row(rr)
            if not rt or not tpl:
                skipped += 1
                continue
            # If org_node is provided, we treat it as the unified hierarchy target
            # and clear legacy org/dir/dept constraints.
            node_id = node.id if node else None
            if node_id is not None:
                org_id = None
                dir_id = None
                dept_id = None
            else:
                org_id = org.id if org else None
                dir_id = direc.id if direc else None
                dept_id = dept.id if dept else None

            existing = WorkflowRoutingRule.query.filter_by(
                request_type_id=rt.id,
                organization_id=org_id,
                directorate_id=dir_id,
                department_id=dept_id,
                org_node_id=node_id,
            ).first()

            if existing:
                existing.template_id = tpl.id
                existing.priority = priority
                existing.is_active = is_active
                existing.org_node_id = node_id
                existing.match_subtree = bool(match_subtree) if node_id is not None else True
                updated += 1
            else:
                db.session.add(WorkflowRoutingRule(
                    request_type_id=rt.id,
                    organization_id=org_id,
                    directorate_id=dir_id,
                    department_id=dept_id,
                    org_node_id=node_id,
                    match_subtree=(bool(match_subtree) if node_id is not None else True),
                    template_id=tpl.id,
                    priority=priority,
                    is_active=is_active,
                ))
                created += 1
        return created, updated

    used_soft = False
    try:
        if mode == "replace":
            def _insert_fn():
                return _upsert_all()

            def _soft():
                WorkflowRoutingRule.query.update({WorkflowRoutingRule.is_active: False})
                db.session.flush()
                return _upsert_all()

            c, u, used_soft = replace_all(db.session, WorkflowRoutingRule.query, _insert_fn, soft_fallback=_soft)
            created, updated = c, u
        else:
            _upsert_all()

        db.session.commit()
    except Exception as e:
        db.session.rollback()
        flash(f"فشل استيراد قواعد التوجيه: {e}", "danger")
        return redirect(url_for("admin.workflow_routing_list"))

    msg = f"تم الاستيراد. تم إنشاء {created} وتحديث {updated}."
    if skipped:
        msg += f" (تم تخطي {skipped} صف/صفوف لغياب نوع الطلب أو المسار أو لعدم التطابق)"
    if mode == "replace" and used_soft:
        msg += " — تم استخدام Soft Replace (تعطيل الكل) بسبب قيود في قاعدة البيانات."
    flash(msg, "success")
    return redirect(url_for("admin.workflow_routing_list"))

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
        node_id = (request.form.get("org_node_id") or "").strip()
        match_subtree = (request.form.get("match_subtree") == "1")

        r.org_node_id = int(node_id) if node_id.isdigit() else None
        r.match_subtree = bool(match_subtree) if r.org_node_id is not None else True

        # If unified OrgNode is set, clear legacy org/dir/dept constraints
        if r.org_node_id is not None:
            r.organization_id = None
            r.directorate_id = None
            r.department_id = None
        else:
            r.organization_id = int(org_id) if org_id.isdigit() else None
            r.directorate_id = int(dir_id) if dir_id.isdigit() else None
            r.department_id = int(dept_id) if dept_id.isdigit() else None

        # priority + active
        try:
            r.priority = int((request.form.get("priority") or "100").strip())
        except Exception:
            r.priority = 100

        r.is_active = (request.form.get("is_active") == "1")

        # validation: don't allow dept without dir, or dir without org (legacy mode only)
        if r.org_node_id is None:
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

# =========================
# Backup & Restore (Full System)
# =========================

def _get_db_path() -> str:
    return os.path.join(current_app.instance_path, "workflow.db")


def _get_project_root() -> str:
    # app.py lives in project root, and Flask root_path points to that directory
    return current_app.root_path


def _get_archive_storage_dir() -> str:
    return os.path.join(_get_project_root(), "storage", "archive")


def _get_portal_uploads_dir() -> str:
    """Portal/admin uploads live under instance/uploads/* (correspondence, store, HR, etc.)."""
    return os.path.join(current_app.instance_path, "uploads")



def _get_static_uploads_dir() -> str:
    """Static uploads live under static/uploads (e.g., user avatars/photos)."""
    return os.path.join(_get_project_root(), "static", "uploads")


def _get_backups_dir() -> str:
    d = os.path.join(current_app.instance_path, "backups")
    os.makedirs(d, exist_ok=True)
    return d


def _create_sqlite_snapshot(src_db_path: str, snapshot_path: str) -> None:
    """Create a consistent snapshot of a SQLite DB using the sqlite3 backup API."""
    os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)

    if not os.path.exists(src_db_path):
        # If DB doesn't exist yet, create an empty snapshot.
        sqlite3.connect(snapshot_path).close()
        return

    src = sqlite3.connect(src_db_path)
    try:
        try:
            # Integrate WAL if enabled
            src.execute("PRAGMA wal_checkpoint(FULL);")
        except Exception:
            pass

        dst = sqlite3.connect(snapshot_path)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()


def _build_backup_zip() -> str:
    """Build a ZIP backup containing DB + storage/archive + portal uploads."""
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backups_dir = _get_backups_dir()

    zip_path = os.path.join(backups_dir, f"workflow_backup_{ts}.zip")
    tmp_db_path = os.path.join(backups_dir, f"workflow_snapshot_{ts}.db")

    # DB snapshot
    _create_sqlite_snapshot(_get_db_path(), tmp_db_path)

    meta = {
        "created_at_utc": datetime.utcnow().isoformat() + "Z",
        "project": "Workflow-PNCECS",
        "includes": [
            "db/workflow.db",
            "storage/archive/*",
            "instance/uploads/*",
            "static/uploads/*",
        ],
    }

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("backup_meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
        z.write(tmp_db_path, "db/workflow.db")

        # Archive storage
        archive_dir = _get_archive_storage_dir()
        if os.path.isdir(archive_dir):
            for root, _dirs, files in os.walk(archive_dir):
                for fn in files:
                    full = os.path.join(root, fn)
                    rel = os.path.relpath(full, _get_project_root())
                    z.write(full, rel)

        # Portal uploads (instance/uploads/*)
        uploads_dir = _get_portal_uploads_dir()
        if os.path.isdir(uploads_dir):
            for root, _dirs, files in os.walk(uploads_dir):
                for fn in files:
                    full = os.path.join(root, fn)
                    rel_inside = os.path.relpath(full, uploads_dir)
                    arcname = os.path.join("instance", "uploads", rel_inside)
                    z.write(full, arcname)



        # Static uploads (static/uploads/*) - e.g., user avatars/photos
        static_uploads_dir = _get_static_uploads_dir()
        if os.path.isdir(static_uploads_dir):
            for root, _dirs, files in os.walk(static_uploads_dir):
                for fn in files:
                    full = os.path.join(root, fn)
                    rel_inside = os.path.relpath(full, static_uploads_dir)
                    arcname = os.path.join("static", "uploads", rel_inside)
                    z.write(full, arcname)
    # cleanup temp snapshot
    try:
        os.remove(tmp_db_path)
    except Exception:
        pass

    return zip_path


def _restore_sqlite_from_snapshot(snapshot_db: str, dest_db: str) -> None:
    """Restore SQLite DB content from snapshot into the destination DB file."""
    os.makedirs(os.path.dirname(dest_db), exist_ok=True)

    # Make SQLAlchemy release connections before overwriting content
    try:
        db.session.remove()
    except Exception:
        pass
    try:
        db.engine.dispose()
    except Exception:
        pass

    src = sqlite3.connect(snapshot_db)
    try:
        dst = sqlite3.connect(dest_db)
        try:
            # Replace destination content
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()


def _copy_tree(src_dir: str, dst_dir: str) -> None:
    os.makedirs(dst_dir, exist_ok=True)
    for root, dirs, files in os.walk(src_dir):
        rel_root = os.path.relpath(root, src_dir)
        target_root = dst_dir if rel_root == "." else os.path.join(dst_dir, rel_root)
        os.makedirs(target_root, exist_ok=True)

        for d in dirs:
            os.makedirs(os.path.join(target_root, d), exist_ok=True)

        for f in files:
            s = os.path.join(root, f)
            t = os.path.join(target_root, f)
            shutil.copy2(s, t)


@admin_bp.route("/backup", methods=["GET"])
@login_required
@roles_required("ADMIN", "SUPER_ADMIN")
def backup_page():
    # show last few backups if exist
    backups_dir = _get_backups_dir()
    backups = []
    try:
        for fn in sorted(os.listdir(backups_dir), reverse=True):
            if fn.lower().endswith(".zip") and fn.startswith("workflow_backup_"):
                backups.append(fn)
            if len(backups) >= 10:
                break
    except Exception:
        backups = []

    return render_template("admin/backup.html", backups=backups)


@admin_bp.route("/backup/download", methods=["GET"])
@login_required
@roles_required("ADMIN", "SUPER_ADMIN")
def backup_download():
    zip_path = _build_backup_zip()
    filename = os.path.basename(zip_path)
    return send_file(
        zip_path,
        as_attachment=True,
        download_name=filename,
        mimetype="application/zip"
    )


@admin_bp.route("/backup/download/<path:fname>", methods=["GET"])
@login_required
@roles_required("ADMIN", "SUPER_ADMIN")
def backup_download_existing(fname):
    # allow downloading previously generated backups (from backups dir only)
    backups_dir = _get_backups_dir()
    safe_name = os.path.basename(fname)
    path = os.path.join(backups_dir, safe_name)

    if not os.path.exists(path) or not safe_name.lower().endswith(".zip"):
        flash("ملف النسخة الاحتياطية غير موجود.", "danger")
        return redirect(url_for("admin.backup_page"))

    return send_file(
        path,
        as_attachment=True,
        download_name=safe_name,
        mimetype="application/zip"
    )


@admin_bp.route("/backup/restore", methods=["POST"])
@login_required
@roles_required("ADMIN", "SUPER_ADMIN")
def backup_restore():
    confirm = request.form.get("confirm_restore")
    if confirm != "1":
        flash("يجب تأكيد الاستيراد قبل المتابعة.", "danger")
        return redirect(url_for("admin.backup_page"))

    up = request.files.get("backup_file")
    if not up or up.filename == "":
        flash("الرجاء اختيار ملف Backup بصيغة ZIP.", "danger")
        return redirect(url_for("admin.backup_page"))

    if not up.filename.lower().endswith(".zip"):
        flash("صيغة الملف غير مدعومة. الرجاء رفع ملف ZIP.", "danger")
        return redirect(url_for("admin.backup_page"))

    backups_dir = _get_backups_dir()
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")

    uploaded_zip = os.path.join(backups_dir, f"uploaded_restore_{ts}.zip")
    up.save(uploaded_zip)

    extract_dir = tempfile.mkdtemp(prefix=f"restore_{ts}_", dir=backups_dir)

    try:
        with zipfile.ZipFile(uploaded_zip, "r") as z:
            z.extractall(extract_dir)
    except Exception:
        flash("ملف النسخة الاحتياطية غير صالح أو تالف.", "danger")
        return redirect(url_for("admin.backup_page"))

    # Locate DB inside backup
    db_candidates = [
        os.path.join(extract_dir, "db", "workflow.db"),
        os.path.join(extract_dir, "instance", "workflow.db"),
        os.path.join(extract_dir, "workflow.db"),
    ]
    snap_db = next((p for p in db_candidates if os.path.exists(p)), None)

    if not snap_db:
        flash("النسخة الاحتياطية لا تحتوي على قاعدة بيانات (workflow.db).", "danger")
        return redirect(url_for("admin.backup_page"))

    # Restore DB content
    try:
        _restore_sqlite_from_snapshot(snap_db, _get_db_path())
    except Exception as e:
        flash(
            "تعذر استيراد قاعدة البيانات. تأكد أن لا يوجد برنامج آخر فاتح workflow.db ثم حاول مرة أخرى.",
            "danger"
        )
        return redirect(url_for("admin.backup_page"))

    # Restore archive storage
    backup_archive = os.path.join(extract_dir, "storage", "archive")
    if os.path.isdir(backup_archive):
        dest_archive = _get_archive_storage_dir()

        # keep current archive as safety copy
        try:
            if os.path.isdir(dest_archive) and os.listdir(dest_archive):
                shutil.move(dest_archive, dest_archive + f"_before_restore_{ts}")
        except Exception:
            # If move fails, we'll merge instead of replace
            pass

        try:
            # If dest doesn't exist now, copy fresh
            if not os.path.isdir(dest_archive):
                _copy_tree(backup_archive, dest_archive)
            else:
                # Merge (copy over)
                _copy_tree(backup_archive, dest_archive)
        except Exception:
            flash("تم استيراد قاعدة البيانات، لكن حدثت مشكلة أثناء استيراد ملفات الأرشفة.", "warning")

    # Restore portal uploads (instance/uploads/*)
    backup_uploads = os.path.join(extract_dir, "instance", "uploads")
    if os.path.isdir(backup_uploads):
        dest_uploads = _get_portal_uploads_dir()

        # keep current uploads as safety copy
        try:
            if os.path.isdir(dest_uploads) and os.listdir(dest_uploads):
                shutil.move(dest_uploads, dest_uploads + f"_before_restore_{ts}")
        except Exception:
            # If move fails, we'll merge instead of replace
            pass

        try:
            if not os.path.isdir(dest_uploads):
                _copy_tree(backup_uploads, dest_uploads)
            else:
                _copy_tree(backup_uploads, dest_uploads)
        except Exception:
            flash("تم استيراد قاعدة البيانات، لكن حدثت مشكلة أثناء استيراد ملفات البوابة الإدارية (uploads).", "warning")


    # Restore static uploads (static/uploads/*)
    backup_static_uploads = os.path.join(extract_dir, "static", "uploads")
    if os.path.isdir(backup_static_uploads):
        dest_static_uploads = _get_static_uploads_dir()

        # keep current static uploads as safety copy
        try:
            if os.path.isdir(dest_static_uploads) and os.listdir(dest_static_uploads):
                shutil.move(dest_static_uploads, dest_static_uploads + f"_before_restore_{ts}")
        except Exception:
            # If move fails, we'll merge instead of replace
            pass

        try:
            if not os.path.isdir(dest_static_uploads):
                _copy_tree(backup_static_uploads, dest_static_uploads)
            else:
                _copy_tree(backup_static_uploads, dest_static_uploads)
        except Exception:
            flash("تم استيراد قاعدة البيانات، لكن حدثت مشكلة أثناء استيراد ملفات static/uploads.", "warning")

    # After restore, force re-login
    try:
        logout_user()
    except Exception:
        pass

    flash("✅ تم استيراد النسخة الاحتياطية بنجاح. يرجى تسجيل الدخول من جديد.", "success")
    flash("ملاحظة: يُفضّل إعادة تشغيل التطبيق بعد الاستيراد لضمان تحديث الاتصالات.", "warning")

    return redirect(url_for("login"))
