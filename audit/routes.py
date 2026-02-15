import json
import re
from flask import render_template, request, send_file
from flask_login import login_required, current_user
from datetime import datetime, timedelta
from sqlalchemy import or_, func, and_
from sqlalchemy.orm import aliased, joinedload

from io import BytesIO
from utils.excel import make_xlsx_bytes, make_xlsx_bytes_multi

from . import audit_bp
from models import (
    AuditLog,
    User,
    WorkflowRequest,
    RequestType,
    WorkflowTemplate,
    WorkflowInstance,
    WorkflowInstanceStep,
    WorkflowStepTask,
)
from extensions import db
from permissions import roles_required


def _apply_message_visibility_filter(query):
    """Hide MESSAGE_* audit entries for non-SUPER_ADMIN."""
    role = (getattr(current_user, "role", "") or "").strip().upper()
    if role != "SUPER_ADMIN":
        query = query.filter(~AuditLog.action.like("MESSAGE_%"))
    return query


@audit_bp.route("/")
@login_required
@roles_required("ADMIN")
def audit_index():
    page = request.args.get("page", 1, type=int)

    q = _apply_message_visibility_filter(AuditLog.query)

    pagination = (
        q.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .paginate(page=page, per_page=20, error_out=False)
    )

    return render_template(
        "audit/index.html",
        logs=pagination.items,
        existing_request_ids=existing_request_ids,
        pagination=pagination
    )


@audit_bp.route("/logs")
@login_required
@roles_required("ADMIN")
def list_audit_logs():
    user_id = request.args.get("user_id")
    action = request.args.get("action")
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    search = request.args.get("search")

    ExecUser = aliased(User)
    BehalfUser = aliased(User)

    query = _apply_message_visibility_filter(
        AuditLog.query
        .outerjoin(ExecUser, AuditLog.user_id == ExecUser.id)
        .outerjoin(BehalfUser, AuditLog.on_behalf_of_id == BehalfUser.id)
        .options(joinedload(AuditLog.user), joinedload(AuditLog.on_behalf_of_user), joinedload(AuditLog.delegation))
    )

    if user_id:
        query = query.filter(AuditLog.user_id == user_id)

    if action:
        query = query.filter(AuditLog.action == action)

    if date_from:
        query = query.filter(
            AuditLog.created_at >= datetime.strptime(date_from, "%Y-%m-%d")
        )

    if date_to:
        query = query.filter(
            AuditLog.created_at <
            datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
        )

    if search:
        for word in search.strip().split():
            query = query.filter(
                or_(
                    AuditLog.note.ilike(f"%{word}%"),
                    AuditLog.action.ilike(f"%{word}%"),
                    or_(
                    ExecUser.email.ilike(f"%{word}%"),
                    BehalfUser.email.ilike(f"%{word}%")
                )
                )
            )

    page = request.args.get("page", 1, type=int)

    pagination = query.order_by(
        AuditLog.created_at.desc(), AuditLog.id.desc()
    ).paginate(page=page, per_page=20, error_out=False)

    users = User.query.order_by(User.email.asc()).all()

    return render_template(
        "audit/list.html",
        logs=pagination.items,
        users=users,
        pagination=pagination
    )


@audit_bp.route("/dashboard")
@login_required
@roles_required("ADMIN")
def audit_dashboard():
    q = _apply_message_visibility_filter(AuditLog.query)

    total_logs = q.count()

    top_users = (
        db.session.query(
            User.email,
            func.count(AuditLog.id)
        )
        .join(AuditLog, AuditLog.user_id == User.id)
    )

    # Hide message logs for non-super admin
    if (getattr(current_user, "role", "") or "").strip().upper() != "SUPER_ADMIN":
        top_users = top_users.filter(~AuditLog.action.like("MESSAGE_%"))

    top_users = (
        top_users
        .group_by(User.email)
        .order_by(func.count(AuditLog.id).desc())
        .limit(5)
        .all()
    )

    top_actions_q = db.session.query(
        AuditLog.action,
        func.count(AuditLog.id)
    )
    if (getattr(current_user, "role", "") or "").strip().upper() != "SUPER_ADMIN":
        top_actions_q = top_actions_q.filter(~AuditLog.action.like("MESSAGE_%"))

    top_actions = (
        top_actions_q
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


@audit_bp.route("/dashboard/export-excel")
@login_required
@roles_required("ADMIN")
def audit_dashboard_export_excel():
    """Export Audit Dashboard aggregates to Excel."""
    q = _apply_message_visibility_filter(AuditLog.query)

    total_logs = q.count()

    top_users_q = (
        db.session.query(User.email, func.count(AuditLog.id))
        .join(AuditLog, AuditLog.user_id == User.id)
    )
    if (getattr(current_user, "role", "") or "").strip().upper() != "SUPER_ADMIN":
        top_users_q = top_users_q.filter(~AuditLog.action.like("MESSAGE_%"))

    top_users_raw = (
        top_users_q
        .group_by(User.email)
        .order_by(func.count(AuditLog.id).desc())
        .limit(50)
        .all()
    )

    top_actions_q = db.session.query(AuditLog.action, func.count(AuditLog.id))
    if (getattr(current_user, "role", "") or "").strip().upper() != "SUPER_ADMIN":
        top_actions_q = top_actions_q.filter(~AuditLog.action.like("MESSAGE_%"))

    top_actions_raw = (
        top_actions_q
        .group_by(AuditLog.action)
        .order_by(func.count(AuditLog.id).desc())
        .limit(50)
        .all()
    )

    top_users = [[email or "(no email)", int(cnt)] for email, cnt in top_users_raw]
    top_actions = [[(action or "-"), int(cnt)] for action, cnt in top_actions_raw]

    sheets = [
        {
            "name": "Summary",
            "headers": ["Metric", "Value"],
            "rows": [
                ["Total Logs", int(total_logs)],
                ["Generated At (UTC)", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")],
            ],
        },
        {
            "name": "Top Users",
            "headers": ["User", "Count"],
            "rows": top_users,
        },
        {
            "name": "Top Actions",
            "headers": ["Action", "Count"],
            "rows": top_actions,
        },
    ]

    xlsx_bytes = make_xlsx_bytes_multi(sheets)
    filename = f"audit_dashboard_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return send_file(
        BytesIO(xlsx_bytes),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )



@audit_bp.route("/timeline")
@login_required
@roles_required("ADMIN")
def system_timeline():
    """High-volume timeline with date range + pagination.

    Default: last 7 days.
    """

    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 120, type=int)
    per_page = max(50, min(per_page, 500))

    action = (request.args.get("action") or "").strip() or None
    user_id = request.args.get("user_id", type=int)
    date_from = (request.args.get("date_from") or "").strip() or None
    date_to = (request.args.get("date_to") or "").strip() or None
    days = request.args.get("days", type=int)

    request_type_id = request.args.get("request_type_id", type=int)
    template_id = request.args.get("template_id", type=int)
    step_order = request.args.get("step_order", type=int)

    # For audit logs: request could be referenced either by request_id, or by target_type/target_id.
    REQUEST_TARGET_TYPES = ["WorkflowRequest", "WORKFLOW_REQUEST", "WORKFLOWREQUEST"]

    base = _apply_message_visibility_filter(
        AuditLog.query.options(
            joinedload(AuditLog.user),
            joinedload(AuditLog.on_behalf_of_user),
            joinedload(AuditLog.delegation),
        )
    )

    if action:
        base = base.filter(AuditLog.action == action)

    if user_id:
        base = base.filter(AuditLog.user_id == user_id)

    # Default time window
    if not date_from and not date_to and not days:
        days = 7

    if days:
        base = base.filter(AuditLog.created_at >= datetime.utcnow() - timedelta(days=days))

    if date_from:
        base = base.filter(AuditLog.created_at >= datetime.strptime(date_from, "%Y-%m-%d"))

    if date_to:
        base = base.filter(
            AuditLog.created_at < datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
        )

    # Request-based filters (نوع الطلب / المسار)
    if request_type_id or template_id:
        req = aliased(WorkflowRequest)
        inst = aliased(WorkflowInstance)

        join_cond = or_(
            AuditLog.request_id == req.id,
            (AuditLog.target_type.in_(REQUEST_TARGET_TYPES) & (AuditLog.target_id == req.id)),
        )

        base = (
            base.outerjoin(req, join_cond)
                # NOTE: WorkflowRequest has no workflow_instance_id column.
                # WorkflowInstance references the request via request_id (unique).
                .outerjoin(inst, inst.request_id == req.id)
        )

        if request_type_id:
            base = base.filter(req.request_type_id == request_type_id)
        if template_id:
            base = base.filter(inst.template_id == template_id)

    # Step filter (الخطوة)
    # We support step-based audit entries coming from:
    # - WorkflowInstanceStep (target_type: WORKFLOW_STEP / WORKFLOW_INSTANCE_STEP)
    # - WorkflowStepTask   (target_type: WORKFLOW_STEP_TASK / PARALLEL_TASK)
    # - Any other entry that encodes step in note (e.g. attachments/escalations) using patterns like step=3 / Step 3 / الخطوة 3
    if step_order:
        stask = aliased(WorkflowStepTask)
        istep = aliased(WorkflowInstanceStep)

        base = base.outerjoin(
            stask,
            and_(
                AuditLog.target_type.in_(["WORKFLOW_STEP_TASK", "PARALLEL_TASK"]),
                AuditLog.target_id == stask.id,
            )
        ).outerjoin(
            istep,
            and_(
                AuditLog.target_type.in_(["WORKFLOW_STEP", "WORKFLOW_INSTANCE_STEP"]),
                AuditLog.target_id == istep.id,
            )
        )

        note_l = func.lower(AuditLog.note)
        patt_eq = f"%step={int(step_order)}%"
        patt_space = f"%step {int(step_order)}%"  # matches 'Step 3' after lower()
        patt_ar = f"%الخطوة {int(step_order)}%"

        base = base.filter(
            or_(
                stask.step_order == int(step_order),
                istep.step_order == int(step_order),
                note_l.like(patt_eq),
                note_l.like(patt_space),
                note_l.like(patt_ar),
            )
        )

    # Summary by day (for quick navigation)
    try:
        day_label = func.strftime('%Y-%m-%d', AuditLog.created_at)
        day_counts = (
            base.with_entities(day_label.label('day'), func.count(AuditLog.id))
            .group_by('day')
            .order_by(day_label.desc())
            .limit(31)
            .all()
        )
    except Exception:
        day_counts = []

    pagination = (
        base.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .paginate(page=page, per_page=per_page, error_out=False)
    )

    logs = list(pagination.items or [])

    users = User.query.order_by(User.email.asc()).all()

    # Action dropdown: keep it light
    actions_q = db.session.query(AuditLog.action).distinct().order_by(AuditLog.action)
    actions_q = _apply_message_visibility_filter(actions_q)
    actions = [a for (a,) in actions_q.limit(200).all()]

    # Dropdown data
    request_types = RequestType.query.order_by(RequestType.name_ar.asc()).all()
    templates = WorkflowTemplate.query.order_by(WorkflowTemplate.name.asc()).all()

    # Helper: effective request id
    def _effective_req_id(l: AuditLog):
        if getattr(l, 'request_id', None):
            return int(l.request_id)
        tt = (getattr(l, 'target_type', None) or '').strip()
        if l.target_id and (tt in REQUEST_TARGET_TYPES):
            try:
                return int(l.target_id)
            except Exception:
                return None
        return None

    page_request_ids = {rid for l in logs for rid in [_effective_req_id(l)] if rid}

    # Existing requests (avoid broken links)
    existing_request_ids = set()
    if page_request_ids:
        existing_request_ids = {
            int(rid) for (rid,) in db.session.query(WorkflowRequest.id)
            .filter(WorkflowRequest.id.in_(page_request_ids)).all()
        }

    # Metadata for displaying request type / template / start & end
    request_meta = {}
    if page_request_ids:
        reqs = (
            WorkflowRequest.query
            .filter(WorkflowRequest.id.in_(page_request_ids))
            .all()
        )

        # start/end timestamps from audit logs (fast enough for current page)
        se_logs = (
            AuditLog.query
            .filter(
                or_(
                    AuditLog.request_id.in_(page_request_ids),
                    (AuditLog.target_type.in_(REQUEST_TARGET_TYPES) & AuditLog.target_id.in_(page_request_ids)),
                )
            )
            .filter(AuditLog.action.in_(["WORKFLOW_STARTED", "WORKFLOW_COMPLETED"]))
            .order_by(AuditLog.created_at.asc())
            .all()
        )
        started = {}
        completed = {}
        for al in se_logs:
            rid = al.request_id or (al.target_id if al.target_type in REQUEST_TARGET_TYPES else None)
            try:
                rid = int(rid) if rid is not None else None
            except Exception:
                rid = None
            if not rid:
                continue
            if al.action == "WORKFLOW_STARTED" and rid not in started:
                started[rid] = al.created_at
            if al.action == "WORKFLOW_COMPLETED":
                completed[rid] = al.created_at

        for r in reqs:
            tpl = None
            try:
                tpl = r.workflow_instance.template if r.workflow_instance else None
            except Exception:
                tpl = None

            rt = getattr(r, 'request_type', None)
            request_meta[int(r.id)] = {
                "request_type": (f"{rt.code} - {rt.name_ar}" if rt else ""),
                "template_name": (tpl.name if tpl else ""),
                "template_id": (tpl.id if tpl else None),
                "started_at": started.get(int(r.id)),
                "completed_at": completed.get(int(r.id)),
            }

    # -----------------
    # Step number for each log (used in UI and step filter UX)
    # -----------------
    def _extract_step_from_note(note: str | None):
        if not note:
            return None
        try:
            s = str(note)
        except Exception:
            return None
        # Common patterns: step=3, Step 3:, الخطوة 3
        m = re.search(r"(?:\bstep\s*=\s*|\bStep\s+)(\d+)", s, flags=re.IGNORECASE)
        if not m:
            m = re.search(r"الخطوة\s*(\d+)", s)
        if not m:
            return None
        try:
            return int(m.group(1))
        except Exception:
            return None

    log_steps = {}
    try:
        task_ids = {
            int(l.target_id) for l in logs
            if l.target_id and ((getattr(l, 'target_type', None) or '').strip() in ['WORKFLOW_STEP_TASK', 'PARALLEL_TASK'])
        }
        step_ids = {
            int(l.target_id) for l in logs
            if l.target_id and ((getattr(l, 'target_type', None) or '').strip() in ['WORKFLOW_STEP', 'WORKFLOW_INSTANCE_STEP'])
        }

        task_step_map = {}
        if task_ids:
            for tid, so in db.session.query(WorkflowStepTask.id, WorkflowStepTask.step_order).filter(WorkflowStepTask.id.in_(task_ids)).all():
                task_step_map[int(tid)] = int(so) if so is not None else None

        inst_step_map = {}
        if step_ids:
            for sid, so in db.session.query(WorkflowInstanceStep.id, WorkflowInstanceStep.step_order).filter(WorkflowInstanceStep.id.in_(step_ids)).all():
                inst_step_map[int(sid)] = int(so) if so is not None else None

        for l in logs:
            tt = ((getattr(l, 'target_type', None) or '').strip())
            st = None
            if tt in ['WORKFLOW_STEP_TASK', 'PARALLEL_TASK'] and l.target_id:
                st = task_step_map.get(int(l.target_id))
            elif tt in ['WORKFLOW_STEP', 'WORKFLOW_INSTANCE_STEP'] and l.target_id:
                st = inst_step_map.get(int(l.target_id))

            if st is None:
                st = _extract_step_from_note(getattr(l, 'note', None))

            if st is not None:
                log_steps[int(l.id)] = int(st)
    except Exception:
        log_steps = {}

    return render_template(
        "audit/timeline.html",
        logs=logs,
        pagination=pagination,
        users=users,
        actions=actions,
        request_types=request_types,
        templates=templates,
        day_counts=day_counts,
        existing_request_ids=existing_request_ids,
        request_meta=request_meta,
        request_times=request_meta,
        log_steps=log_steps,
        req_target_types=REQUEST_TARGET_TYPES,
        filters={
            "action": action or "",
            "user_id": user_id or "",
            "date_from": date_from or "",
            "date_to": date_to or "",
            "days": days or "",
            "per_page": per_page,
            "request_type_id": request_type_id or "",
            "template_id": template_id or "",
            "step_order": step_order or "",
        }
    )




@audit_bp.route("/request/<int:request_id>/snapshot")
@login_required
@roles_required("ADMIN")
def deleted_request_snapshot(request_id):
    """Show snapshot and audit history for a request that was deleted.

    The deletion event stores a JSON snapshot inside AuditLog.note (SNAPSHOT_JSON:...).
    """
    # If the request still exists, redirect to the normal view
    if WorkflowRequest.query.get(request_id):
        # Use workflow view if available
        try:
            from flask import redirect, url_for
            return redirect(url_for("workflow.view_request", request_id=request_id))
        except Exception:
            pass

    del_log = (
        AuditLog.query
        .options(
            joinedload(AuditLog.user),
            joinedload(AuditLog.on_behalf_of_user),
            joinedload(AuditLog.delegation),
        )
        .filter(AuditLog.action == "REQUEST_DELETED")
        .filter(AuditLog.target_type.in_(["WorkflowRequest","WORKFLOW_REQUEST"]))
        .filter(AuditLog.target_id == request_id)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .first()
    )

    snapshot = None
    if del_log and del_log.note and "SNAPSHOT_JSON:" in del_log.note:
        raw = del_log.note.split("SNAPSHOT_JSON:", 1)[1].strip()
        try:
            snapshot = json.loads(raw)
        except Exception:
            snapshot = None

    logs = (
        AuditLog.query
        .options(
            joinedload(AuditLog.user),
            joinedload(AuditLog.on_behalf_of_user),
            joinedload(AuditLog.delegation),
        )
        .filter(AuditLog.target_type.in_(["WorkflowRequest","WORKFLOW_REQUEST"]))
        .filter(AuditLog.target_id == request_id)
        .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        .all()
    )

    return render_template(
        "audit/deleted_request_snapshot.html",
        request_id=request_id,
        del_log=del_log,
        snapshot=snapshot,
        logs=logs,
    )


@audit_bp.route("/timeline/export.xlsx")
@login_required
@roles_required("ADMIN")
def system_timeline_export_excel():
    """Export timeline to Excel honoring the same filters."""
    action = (request.args.get("action") or "").strip() or None
    user_id = request.args.get("user_id", type=int)
    date_from = (request.args.get("date_from") or "").strip() or None
    date_to = (request.args.get("date_to") or "").strip() or None
    days = request.args.get("days", type=int)
    request_type_id = request.args.get("request_type_id", type=int)
    template_id = request.args.get("template_id", type=int)
    step_order = request.args.get("step_order", type=int)

    REQUEST_TARGET_TYPES = ["WorkflowRequest", "WORKFLOW_REQUEST", "WORKFLOWREQUEST"]

    q = _apply_message_visibility_filter(
        AuditLog.query.options(
            joinedload(AuditLog.user),
            joinedload(AuditLog.on_behalf_of_user),
            joinedload(AuditLog.delegation),
        )
    )

    if action:
        q = q.filter(AuditLog.action == action)
    if user_id:
        q = q.filter(AuditLog.user_id == user_id)

    if not date_from and not date_to and not days:
        days = 7
    if days:
        q = q.filter(AuditLog.created_at >= datetime.utcnow() - timedelta(days=days))
    if date_from:
        q = q.filter(AuditLog.created_at >= datetime.strptime(date_from, "%Y-%m-%d"))
    if date_to:
        q = q.filter(AuditLog.created_at < datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1))

    if request_type_id or template_id:
        req = aliased(WorkflowRequest)
        inst = aliased(WorkflowInstance)
        join_cond = or_(
            AuditLog.request_id == req.id,
            (AuditLog.target_type.in_(REQUEST_TARGET_TYPES) & (AuditLog.target_id == req.id)),
        )
        # NOTE: WorkflowRequest has no workflow_instance_id column.
        q = q.outerjoin(req, join_cond).outerjoin(inst, inst.request_id == req.id)
        if request_type_id:
            q = q.filter(req.request_type_id == request_type_id)
        if template_id:
            q = q.filter(inst.template_id == template_id)

    # Step filter (الخطوة)
    if step_order:
        stask = aliased(WorkflowStepTask)
        istep = aliased(WorkflowInstanceStep)

        q = q.outerjoin(
            stask,
            and_(
                AuditLog.target_type.in_(["WORKFLOW_STEP_TASK", "PARALLEL_TASK"]),
                AuditLog.target_id == stask.id,
            )
        ).outerjoin(
            istep,
            and_(
                AuditLog.target_type.in_(["WORKFLOW_STEP", "WORKFLOW_INSTANCE_STEP"]),
                AuditLog.target_id == istep.id,
            )
        )

        note_l = func.lower(AuditLog.note)
        patt_eq = f"%step={int(step_order)}%"
        patt_space = f"%step {int(step_order)}%"
        patt_ar = f"%الخطوة {int(step_order)}%"
        q = q.filter(
            or_(
                stask.step_order == int(step_order),
                istep.step_order == int(step_order),
                note_l.like(patt_eq),
                note_l.like(patt_space),
                note_l.like(patt_ar),
            )
        )

    logs = q.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(20000).all()

    # Collect request ids
    def _effective_req_id(l: AuditLog):
        if getattr(l, 'request_id', None):
            return int(l.request_id)
        tt = (getattr(l, 'target_type', None) or '').strip()
        if l.target_id and (tt in REQUEST_TARGET_TYPES):
            try:
                return int(l.target_id)
            except Exception:
                return None
        return None

    req_ids = {rid for l in logs for rid in [_effective_req_id(l)] if rid}

    request_meta = {}
    if req_ids:
        reqs = WorkflowRequest.query.filter(WorkflowRequest.id.in_(req_ids)).all()

        se_logs = (
            AuditLog.query
            .filter(
                or_(
                    AuditLog.request_id.in_(req_ids),
                    (AuditLog.target_type.in_(REQUEST_TARGET_TYPES) & AuditLog.target_id.in_(req_ids)),
                )
            )
            .filter(AuditLog.action.in_(["WORKFLOW_STARTED", "WORKFLOW_COMPLETED"]))
            .order_by(AuditLog.created_at.asc())
            .all()
        )
        started = {}
        completed = {}
        for al in se_logs:
            rid = al.request_id or (al.target_id if al.target_type in REQUEST_TARGET_TYPES else None)
            try:
                rid = int(rid) if rid is not None else None
            except Exception:
                rid = None
            if not rid:
                continue
            if al.action == "WORKFLOW_STARTED" and rid not in started:
                started[rid] = al.created_at
            if al.action == "WORKFLOW_COMPLETED":
                completed[rid] = al.created_at

        for r in reqs:
            tpl = None
            try:
                tpl = r.workflow_instance.template if r.workflow_instance else None
            except Exception:
                tpl = None
            rt = getattr(r, 'request_type', None)
            request_meta[int(r.id)] = {
                "request_type": (f"{rt.code} - {rt.name_ar}" if rt else ""),
                "template_name": (tpl.name if tpl else ""),
                "started_at": started.get(int(r.id)),
                "completed_at": completed.get(int(r.id)),
            }

    # Step number map for export (batch)
    def _extract_step_from_note(note):
        if not note:
            return None
        try:
            s = str(note)
        except Exception:
            return None
        m = re.search(r"(?:\\bstep\\s*=\\s*|\\bStep\\s+)(\\d+)", s, flags=re.IGNORECASE)
        if not m:
            m = re.search(r"الخطوة\\s*(\\d+)", s)
        if not m:
            return None
        try:
            return int(m.group(1))
        except Exception:
            return None

    task_ids = {
        int(l.target_id) for l in logs
        if l.target_id and ((getattr(l, 'target_type', None) or '').strip() in ['WORKFLOW_STEP_TASK', 'PARALLEL_TASK'])
    }
    step_ids = {
        int(l.target_id) for l in logs
        if l.target_id and ((getattr(l, 'target_type', None) or '').strip() in ['WORKFLOW_STEP', 'WORKFLOW_INSTANCE_STEP'])
    }
    task_step_map = {}
    if task_ids:
        for tid, so in db.session.query(WorkflowStepTask.id, WorkflowStepTask.step_order).filter(WorkflowStepTask.id.in_(task_ids)).all():
            task_step_map[int(tid)] = int(so) if so is not None else None
    inst_step_map = {}
    if step_ids:
        for sid, so in db.session.query(WorkflowInstanceStep.id, WorkflowInstanceStep.step_order).filter(WorkflowInstanceStep.id.in_(step_ids)).all():
            inst_step_map[int(sid)] = int(so) if so is not None else None

    rows = []
    for l in logs:
        rid = _effective_req_id(l)
        meta = request_meta.get(rid or -1, {})

        # Resolve step number if possible
        st = None
        tt = ((getattr(l, 'target_type', None) or '').strip())
        if tt in ['WORKFLOW_STEP_TASK', 'PARALLEL_TASK'] and l.target_id:
            st = task_step_map.get(int(l.target_id))
        elif tt in ['WORKFLOW_STEP', 'WORKFLOW_INSTANCE_STEP'] and l.target_id:
            st = inst_step_map.get(int(l.target_id))
        if st is None:
            st = _extract_step_from_note(getattr(l, 'note', None))

        rows.append({
            "ID": l.id,
            "Time": l.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            "Action": l.action,
            "User": (l.user.email if l.user else 'System'),
            "On behalf of": (l.on_behalf_of_user.email if l.on_behalf_of_user else ''),
            "Request ID": rid or '',
            "Request Type": meta.get('request_type', ''),
            "Template": meta.get('template_name', ''),
            "Step": (st if st is not None else ''),
            "Workflow Started": (meta.get('started_at').strftime('%Y-%m-%d %H:%M:%S') if meta.get('started_at') else ''),
            "Workflow Completed": (meta.get('completed_at').strftime('%Y-%m-%d %H:%M:%S') if meta.get('completed_at') else ''),
            "Target Type": l.target_type or '',
            "Target ID": l.target_id or '',
            "Note": (l.note or ''),
        })

    content = make_xlsx_bytes("Timeline", rows)
    filename = f"system_timeline_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.xlsx"

    return send_file(
        BytesIO(content),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )
