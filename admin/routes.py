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
from models import WorkflowRequest, SystemSetting, AuditLog, ArchivedFile, WorkflowRoutingRule, RequestType, Organization, Directorate, Department, WorkflowTemplate, RequestEscalation
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
    """Build a ZIP backup containing DB + storage/archive."""
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backups_dir = _get_backups_dir()

    zip_path = os.path.join(backups_dir, f"workflow_backup_{ts}.zip")
    tmp_db_path = os.path.join(backups_dir, f"workflow_snapshot_{ts}.db")

    # DB snapshot
    _create_sqlite_snapshot(_get_db_path(), tmp_db_path)

    meta = {
        "created_at_utc": datetime.utcnow().isoformat() + "Z",
        "project": "Workflow-PNCECS",
        "includes": ["db/workflow.db", "storage/archive/*"],
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

    # After restore, force re-login
    try:
        logout_user()
    except Exception:
        pass

    flash("✅ تم استيراد النسخة الاحتياطية بنجاح. يرجى تسجيل الدخول من جديد.", "success")
    flash("ملاحظة: يُفضّل إعادة تشغيل التطبيق بعد الاستيراد لضمان تحديث الاتصالات.", "warning")

    return redirect(url_for("login"))
