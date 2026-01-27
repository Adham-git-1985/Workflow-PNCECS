# workflow/templates_admin.py

from flask import render_template, request, redirect, url_for, flash, send_file
from flask_login import login_required, current_user
from sqlalchemy import func

from io import BytesIO
from datetime import datetime

import openpyxl
from openpyxl.styles import Font, Alignment
from openpyxl.utils import get_column_letter

from . import workflow_bp
from extensions import db
from utils.perms import perm_required
from models import (
    WorkflowTemplate,
    WorkflowTemplateStep,
    User,
    Department,
    Directorate,
    Role,
)

def _xlsx_response(sheet_name: str, headers: list[str], rows: list[list], filename_prefix: str):
    # Create and return an .xlsx download.
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name[:31]  # Excel sheet name max length

    header_font = Font(bold=True)
    header_alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)

    ws.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.alignment = header_alignment

    for row in rows:
        ws.append(row)

    # basic auto-width (capped)
    for col_idx in range(1, len(headers) + 1):
        col_letter = get_column_letter(col_idx)
        max_len = 10
        for cell in ws[col_letter]:
            val = '' if cell.value is None else str(cell.value)
            if len(val) > max_len:
                max_len = len(val)
        ws.column_dimensions[col_letter].width = min(max_len, 60) + 2

    ws.freeze_panes = 'A2'

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)

    ts = datetime.now().strftime('%Y-%m-%d_%H-%M')
    filename = f"{filename_prefix}_{ts}.xlsx"

    return send_file(
        bio,
        as_attachment=True,
        download_name=filename,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )



def _to_int(val, default=None):
    try:
        if val is None or str(val).strip() == "":
            return default
        return int(val)
    except Exception:
        return default


def _normalize_kind(kind: str) -> str:
    kind = (kind or "").strip().upper()
    if kind in ("USER", "ROLE", "DEPARTMENT", "DIRECTORATE"):
        return kind
    return ""


def _get_role_choices():
    """Return role list for UI (active roles table + any roles already in DB)."""
    seen = set()
    out = []

    def add(r):
        if not r:
            return
        r = str(r).strip()
        if not r:
            return
        key = r.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(r)

    # 1) Preferred source: active roles from master data
    try:
        for r in Role.query.filter_by(is_active=True).order_by(Role.code.asc()).all():
            add(r.code)
    except Exception:
        pass

    # 2) Fallback: any roles already used by users
    for (r,) in db.session.query(User.role).distinct().all():
        add(r)

    # 3) Ensure core system roles exist
    for r in ["USER", "ADMIN", "SUPER_ADMIN"]:
        add(r)

    return out


def _resequence_steps(template_id: int):
    steps = (
        WorkflowTemplateStep.query.filter_by(template_id=template_id)
        .order_by(WorkflowTemplateStep.step_order.asc())
        .all()
    )
    for i, s in enumerate(steps, start=1):
        s.step_order = i
    db.session.commit()


@workflow_bp.route("/templates")
@login_required
@perm_required("WORKFLOW_TEMPLATES_READ")
def templates_list():
    q = (request.args.get("q") or "").strip()

    query = WorkflowTemplate.query
    if q:
        like = f"%{q}%"
        query = query.filter(WorkflowTemplate.name.ilike(like))

    items = query.order_by(WorkflowTemplate.id.desc()).all()
    return render_template("workflow/templates_admin/list.html", items=items, q=q)


@workflow_bp.route('/templates/export-excel')
@login_required
@perm_required('WORKFLOW_TEMPLATES_READ')
def templates_export_excel():
    """Export templates list to Excel (respects ?q= filter)."""
    q = (request.args.get('q') or '').strip()

    query = WorkflowTemplate.query
    if q:
        like = f"%{q}%"
        query = query.filter(WorkflowTemplate.name.ilike(like))

    items = query.order_by(WorkflowTemplate.id.desc()).all()

    headers = ['ID', 'الاسم', 'نشط', 'SLA الافتراضي (أيام)', 'عدد الخطوات']
    rows = []
    for t in items:
        steps_count = (
            db.session.query(func.count(WorkflowTemplateStep.id))
            .filter(WorkflowTemplateStep.template_id == t.id)
            .scalar()
        ) or 0

        rows.append([
            t.id,
            t.name,
            'نعم' if t.is_active else 'لا',
            t.sla_days_default if t.sla_days_default is not None else '',
            int(steps_count),
        ])

    return _xlsx_response(
        sheet_name='Templates',
        headers=headers,
        rows=rows,
        filename_prefix='workflow_templates',
    )


@workflow_bp.route('/templates/<int:template_id>/steps/export-excel')
@login_required
@perm_required('WORKFLOW_TEMPLATES_READ')
def templates_steps_export_excel(template_id):
    """Export steps for one template to Excel."""
    t = WorkflowTemplate.query.get_or_404(template_id)

    steps = (
        WorkflowTemplateStep.query.filter_by(template_id=t.id)
        .order_by(WorkflowTemplateStep.step_order.asc())
        .all()
    )

    users = User.query.order_by(User.email.asc()).all()
    users_map = {u.id: u for u in users}

    departments = Department.query.order_by(Department.name_ar.asc()).all()
    depts_map = {d.id: d for d in departments}

    directorates = Directorate.query.order_by(Directorate.name_ar.asc()).all()
    dirs_map = {d.id: d for d in directorates}

    headers = ['Template ID', 'Template Name', 'Step', 'Kind', 'Target', 'SLA (أيام)']
    rows = []
    for s in steps:
        target = ''
        if s.approver_kind == 'USER':
            u = users_map.get(s.approver_user_id)
            target = u.email if u else (s.approver_user_id or '')
        elif s.approver_kind == 'ROLE':
            target = s.approver_role or ''
        elif s.approver_kind == 'DEPARTMENT':
            d = depts_map.get(s.approver_department_id)
            target = d.name_ar if d else (s.approver_department_id or '')
        elif s.approver_kind == 'DIRECTORATE':
            d = dirs_map.get(s.approver_directorate_id)
            target = d.name_ar if d else (s.approver_directorate_id or '')

        rows.append([
            t.id,
            t.name,
            s.step_order,
            s.approver_kind,
            target,
            s.sla_days if s.sla_days is not None else '',
        ])

    return _xlsx_response(
        sheet_name='Template Steps',
        headers=headers,
        rows=rows,
        filename_prefix=f'template_{t.id}_steps',
    )


@workflow_bp.route("/templates/new", methods=["GET", "POST"])
@login_required
@perm_required("WORKFLOW_TEMPLATES_CREATE")
def templates_new():
    users = User.query.order_by(User.email.asc()).all()
    departments = (
        Department.query.filter_by(is_active=True).order_by(Department.name_ar.asc()).all()
    )
    directorates = (
        Directorate.query.filter_by(is_active=True)
        .order_by(Directorate.name_ar.asc())
        .all()
    )
    role_choices = _get_role_choices()

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        sla_days_default = _to_int(request.form.get("sla_days_default"), default=None)

        if not name:
            flash("اسم المسار مطلوب.", "danger")
            return redirect(request.url)

        t = WorkflowTemplate(
            name=name,
            is_active=True,  # new.html عادة بدون checkbox
            created_by_id=current_user.id,
            sla_days_default=sla_days_default,
        )
        db.session.add(t)
        db.session.flush()  # نحتاج t.id قبل إضافة الخطوات

        # Arrays from new.html
        kinds = request.form.getlist("approver_kind")
        user_ids = request.form.getlist("approver_user_id")
        roles = request.form.getlist("approver_role")
        dept_ids = request.form.getlist("approver_department_id")
        dir_ids = request.form.getlist("approver_directorate_id")
        sla_list = request.form.getlist("step_sla_days")

        max_len = max(
            len(kinds),
            len(user_ids),
            len(roles),
            len(dept_ids),
            len(dir_ids),
            len(sla_list),
            0,
        )

        step_order = 1

        for i in range(max_len):
            kind = _normalize_kind(kinds[i] if i < len(kinds) else "")
            user_id = _to_int(user_ids[i] if i < len(user_ids) else "", default=None)
            role = (roles[i] if i < len(roles) else "").strip()
            dept_id = _to_int(dept_ids[i] if i < len(dept_ids) else "", default=None)
            dir_id = _to_int(dir_ids[i] if i < len(dir_ids) else "", default=None)
            sla_days = _to_int(sla_list[i] if i < len(sla_list) else "", default=None)

            # Skip completely empty row
            if (
                not kind
                and not user_id
                and not role
                and not dept_id
                and not dir_id
                and sla_days is None
            ):
                continue

            if not kind:
                db.session.rollback()
                flash("يرجى اختيار نوع المعتمد لكل خطوة.", "danger")
                return redirect(request.url)

            if kind == "USER" and not user_id:
                db.session.rollback()
                flash("يرجى اختيار مستخدم لخطوة نوع USER.", "danger")
                return redirect(request.url)

            if kind == "ROLE" and not role:
                db.session.rollback()
                flash("يرجى اختيار ROLE لخطوة نوع ROLE.", "danger")
                return redirect(request.url)

            if kind == "DEPARTMENT" and not dept_id:
                db.session.rollback()
                flash("يرجى اختيار دائرة/قسم للخطوة.", "danger")
                return redirect(request.url)

            if kind == "DIRECTORATE" and not dir_id:
                db.session.rollback()
                flash("يرجى اختيار إدارة للخطوة.", "danger")
                return redirect(request.url)

            step = WorkflowTemplateStep(
                template_id=t.id,
                step_order=step_order,
                approver_kind=kind,
                approver_user_id=user_id if kind == "USER" else None,
                approver_role=role if kind == "ROLE" else None,
                approver_department_id=dept_id if kind == "DEPARTMENT" else None,
                approver_directorate_id=dir_id if kind == "DIRECTORATE" else None,
                sla_days=sla_days,
            )
            db.session.add(step)
            step_order += 1

        if step_order == 1:
            db.session.rollback()
            flash("أضف خطوة واحدة على الأقل.", "danger")
            return redirect(request.url)

        db.session.commit()
        flash("تم إنشاء المسار وخطواته.", "success")
        return redirect(url_for("workflow.templates_edit", template_id=t.id))

    return render_template(
        "workflow/templates_admin/new.html",
        users=users,
        departments=departments,
        directorates=directorates,
        role_choices=role_choices,
    )


@workflow_bp.route("/templates/<int:template_id>/edit", methods=["GET", "POST"])
@login_required
@perm_required("WORKFLOW_TEMPLATES_UPDATE")
def templates_edit(template_id):
    t = WorkflowTemplate.query.get_or_404(template_id)

    if request.method == "POST":
        t.name = (request.form.get("name") or "").strip()
        t.is_active = request.form.get("is_active") == "1"
        t.sla_days_default = _to_int(request.form.get("sla_days_default"), default=None)

        if not t.name:
            flash("اسم المسار مطلوب.", "danger")
            return redirect(request.url)

        db.session.commit()
        flash("تم حفظ بيانات المسار.", "success")
        return redirect(url_for("workflow.templates_edit", template_id=t.id))

    steps = (
        WorkflowTemplateStep.query.filter_by(template_id=t.id)
        .order_by(WorkflowTemplateStep.step_order.asc())
        .all()
    )

    users = User.query.order_by(User.email.asc()).all()
    departments = (
        Department.query.filter_by(is_active=True).order_by(Department.name_ar.asc()).all()
    )
    directorates = (
        Directorate.query.filter_by(is_active=True)
        .order_by(Directorate.name_ar.asc())
        .all()
    )
    role_choices = _get_role_choices()

    return render_template(
        "workflow/templates_admin/edit.html",
        t=t,
        steps=steps,
        users=users,
        departments=departments,
        directorates=directorates,
        role_choices=role_choices,
    )


@workflow_bp.route("/templates/<int:template_id>/details")
@login_required
@perm_required("WORKFLOW_TEMPLATES_READ")
def templates_details(template_id):
    t = WorkflowTemplate.query.get_or_404(template_id)

    steps = (
        WorkflowTemplateStep.query.filter_by(template_id=t.id)
        .order_by(WorkflowTemplateStep.step_order.asc())
        .all()
    )

    users = User.query.order_by(User.email.asc()).all()
    users_map = {u.id: u for u in users}

    departments = (
        Department.query.filter_by(is_active=True).order_by(Department.name_ar.asc()).all()
    )
    depts_map = {d.id: d for d in departments}

    directorates = (
        Directorate.query.filter_by(is_active=True)
        .order_by(Directorate.name_ar.asc())
        .all()
    )
    dirs_map = {d.id: d for d in directorates}

    return render_template(
        "workflow/templates_admin/details.html",
        t=t,
        steps=steps,
        users_map=users_map,
        depts_map=depts_map,
        dirs_map=dirs_map,
    )


@workflow_bp.route("/templates/<int:template_id>/delete", methods=["POST"])
@login_required
@perm_required("WORKFLOW_TEMPLATES_DELETE")
def templates_delete(template_id):
    t = WorkflowTemplate.query.get_or_404(template_id)

    WorkflowTemplateStep.query.filter_by(template_id=t.id).delete()
    db.session.delete(t)
    db.session.commit()

    flash("تم حذف المسار.", "warning")
    return redirect(url_for("workflow.templates_list"))


@workflow_bp.route("/templates/<int:template_id>/steps/add", methods=["POST"])
@login_required
@perm_required("WORKFLOW_TEMPLATES_UPDATE")
def templates_steps_add(template_id):
    t = WorkflowTemplate.query.get_or_404(template_id)

    kind = _normalize_kind(request.form.get("approver_kind"))
    user_id = _to_int(request.form.get("approver_user_id"), default=None)
    dept_id = _to_int(request.form.get("approver_department_id"), default=None)
    dir_id = _to_int(request.form.get("approver_directorate_id"), default=None)
    role = (request.form.get("approver_role") or "").strip()
    sla_days = _to_int(request.form.get("sla_days"), default=None)

    if not kind:
        flash("يرجى اختيار نوع المعتمد (USER/ROLE/DEPARTMENT/DIRECTORATE).", "danger")
        return redirect(url_for("workflow.templates_edit", template_id=t.id))

    if kind == "USER" and not user_id:
        flash("يرجى اختيار مستخدم للخطوة.", "danger")
        return redirect(url_for("workflow.templates_edit", template_id=t.id))

    if kind == "ROLE" and not role:
        flash("يرجى إدخال ROLE للخطوة.", "danger")
        return redirect(url_for("workflow.templates_edit", template_id=t.id))

    if kind == "DEPARTMENT" and not dept_id:
        flash("يرجى اختيار دائرة/قسم للخطوة.", "danger")
        return redirect(url_for("workflow.templates_edit", template_id=t.id))

    if kind == "DIRECTORATE" and not dir_id:
        flash("يرجى اختيار إدارة للخطوة.", "danger")
        return redirect(url_for("workflow.templates_edit", template_id=t.id))

    max_order = (
        db.session.query(func.max(WorkflowTemplateStep.step_order))
        .filter(WorkflowTemplateStep.template_id == t.id)
        .scalar()
    ) or 0

    step = WorkflowTemplateStep(
        template_id=t.id,
        step_order=int(max_order) + 1,
        approver_kind=kind,
        approver_user_id=user_id if kind == "USER" else None,
        approver_department_id=dept_id if kind == "DEPARTMENT" else None,
        approver_directorate_id=dir_id if kind == "DIRECTORATE" else None,
        approver_role=role if kind == "ROLE" else None,
        sla_days=sla_days,
    )
    db.session.add(step)
    db.session.commit()

    flash("تمت إضافة خطوة.", "success")
    return redirect(url_for("workflow.templates_edit", template_id=t.id))


@workflow_bp.route("/templates/steps/<int:step_id>/move/<direction>", methods=["POST"])
@login_required
@perm_required("WORKFLOW_TEMPLATES_UPDATE")
def templates_steps_move(step_id, direction):
    s = WorkflowTemplateStep.query.get_or_404(step_id)
    tid = s.template_id

    direction = (direction or "").strip().lower()
    if direction not in ("up", "down"):
        flash("اتجاه غير صحيح.", "danger")
        return redirect(url_for("workflow.templates_edit", template_id=tid))

    target_order = (s.step_order or 1) - 1 if direction == "up" else (s.step_order or 1) + 1

    other = (
        WorkflowTemplateStep.query
        .filter_by(template_id=tid, step_order=target_order)
        .first()
    )
    if other:
        other.step_order, s.step_order = s.step_order, other.step_order
        db.session.commit()
        _resequence_steps(tid)

    flash("تم تحديث ترتيب الخطوات.", "success")
    return redirect(url_for("workflow.templates_edit", template_id=tid))


@workflow_bp.route("/templates/steps/<int:step_id>/delete", methods=["POST"])
@login_required
@perm_required("WORKFLOW_TEMPLATES_UPDATE")
def templates_steps_delete(step_id):
    s = WorkflowTemplateStep.query.get_or_404(step_id)
    tid = s.template_id

    db.session.delete(s)
    db.session.commit()

    _resequence_steps(tid)
    flash("تم حذف الخطوة.", "warning")
    return redirect(url_for("workflow.templates_edit", template_id=tid))


@workflow_bp.route("/templates/steps/<int:step_id>/edit", methods=["GET", "POST"])
@login_required
@perm_required("WORKFLOW_TEMPLATES_UPDATE")
def templates_steps_edit(step_id):
    s = WorkflowTemplateStep.query.get_or_404(step_id)
    t = WorkflowTemplate.query.get_or_404(s.template_id)

    users = User.query.order_by(User.email.asc()).all()
    departments = (
        Department.query.filter_by(is_active=True).order_by(Department.name_ar.asc()).all()
    )
    directorates = (
        Directorate.query.filter_by(is_active=True)
        .order_by(Directorate.name_ar.asc())
        .all()
    )
    role_choices = _get_role_choices()

    if request.method == "POST":
        kind = _normalize_kind(request.form.get("approver_kind"))
        user_id = _to_int(request.form.get("approver_user_id"), default=None)
        dept_id = _to_int(request.form.get("approver_department_id"), default=None)
        dir_id = _to_int(request.form.get("approver_directorate_id"), default=None)
        role = (request.form.get("approver_role") or "").strip()
        sla_days = _to_int(request.form.get("sla_days"), default=None)

        if not kind:
            flash("نوع المعتمد غير صحيح.", "danger")
            return redirect(request.url)

        if kind == "USER" and not user_id:
            flash("يرجى اختيار مستخدم.", "danger")
            return redirect(request.url)

        if kind == "ROLE" and not role:
            flash("يرجى إدخال ROLE.", "danger")
            return redirect(request.url)

        if kind == "DEPARTMENT" and not dept_id:
            flash("يرجى اختيار دائرة/قسم للخطوة.", "danger")
            return redirect(request.url)

        if kind == "DIRECTORATE" and not dir_id:
            flash("يرجى اختيار إدارة للخطوة.", "danger")
            return redirect(request.url)

        s.approver_kind = kind
        s.approver_user_id = user_id if kind == "USER" else None
        s.approver_role = role if kind == "ROLE" else None
        s.approver_department_id = dept_id if kind == "DEPARTMENT" else None
        s.approver_directorate_id = dir_id if kind == "DIRECTORATE" else None
        s.sla_days = sla_days

        db.session.commit()
        flash("تم تحديث الخطوة.", "success")
        return redirect(url_for("workflow.templates_edit", template_id=t.id))

    return render_template(
        "workflow/templates_admin/step_edit.html",
        t=t,
        s=s,
        users=users,
        departments=departments,
        directorates=directorates,
        role_choices=role_choices,
    )
