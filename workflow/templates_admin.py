# workflow/templates_admin.py

from flask import render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from sqlalchemy import func

from . import workflow_bp
from extensions import db
from utils.perms import perm_required

from models import (
    WorkflowTemplate,
    WorkflowTemplateStep,
    User,
    Department,
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
    if kind in ("USER", "ROLE", "DEPARTMENT"):
        return kind
    return ""


def _get_role_choices():
    """Return role list for UI (stable + any roles already in DB)."""
    base = [
        "USER",
        "dept_head",
        "deputy_head",
        "finance",
        "secretary_general",
        "ADMIN",
        "SUPER_ADMIN",
    ]

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

    for r in base:
        add(r)

    for (r,) in db.session.query(User.role).distinct().all():
        add(r)

    return out


def _resequence_steps(template_id: int):
    steps = (
        WorkflowTemplateStep.query
        .filter_by(template_id=template_id)
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
    items = (
        WorkflowTemplate.query
        .order_by(WorkflowTemplate.id.desc())
        .all()
    )
    return render_template("workflow/templates_admin/list.html", items=items)


@workflow_bp.route("/templates/new", methods=["GET", "POST"])
@login_required
@perm_required("WORKFLOW_TEMPLATES_CREATE")
def templates_new():
    users = User.query.order_by(User.email.asc()).all()
    departments = Department.query.filter_by(is_active=True).order_by(Department.name_ar.asc()).all()
    role_choices = _get_role_choices()

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        sla_days_default = _to_int(request.form.get("sla_days_default"), default=None)

        if not name:
            flash("اسم المسار مطلوب.", "danger")
            return redirect(request.url)

        t = WorkflowTemplate(
            name=name,
            is_active=True,  # new.html ما فيه checkbox
            created_by_id=current_user.id,
            sla_days_default=sla_days_default
        )
        db.session.add(t)
        db.session.flush()  # نحتاج t.id

        # ====== steps arrays from new.html ======
        kinds = request.form.getlist("approver_kind")
        user_ids = request.form.getlist("approver_user_id")
        roles = request.form.getlist("approver_role")
        dept_ids = request.form.getlist("approver_department_id")
        sla_list = request.form.getlist("step_sla_days")  # نفس الاسم في new.html

        max_len = max(len(kinds), len(user_ids), len(roles), len(dept_ids), len(sla_list), 0)

        step_order = 1
        for i in range(max_len):
            kind = _normalize_kind(kinds[i] if i < len(kinds) else "")
            user_id = _to_int(user_ids[i] if i < len(user_ids) else "", default=None)
            role = (roles[i] if i < len(roles) else "").strip()
            dept_id = _to_int(dept_ids[i] if i < len(dept_ids) else "", default=None)
            sla_days = _to_int(sla_list[i] if i < len(sla_list) else "", default=None)

            # skip completely empty rows
            if not kind and not user_id and not role and not dept_id and sla_days is None:
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
                flash("يرجى إدخال ROLE لخطوة نوع ROLE.", "danger")
                return redirect(request.url)

            if kind == "DEPARTMENT" and not dept_id:
                db.session.rollback()
                flash("يرجى إدخال Department ID لخطوة نوع DEPARTMENT.", "danger")
                return redirect(request.url)

            step = WorkflowTemplateStep(
                template_id=t.id,
                step_order=step_order,
                approver_kind=kind,
                approver_user_id=user_id if kind == "USER" else None,
                approver_role=role if kind == "ROLE" else None,
                approver_department_id=dept_id if kind == "DEPARTMENT" else None,
                sla_days=sla_days
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

    return render_template("workflow/templates_admin/new.html", users=users, departments=departments, role_choices=role_choices)


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
        WorkflowTemplateStep.query
        .filter_by(template_id=t.id)
        .order_by(WorkflowTemplateStep.step_order.asc())
        .all()
    )

    users = User.query.order_by(User.email.asc()).all()
    departments = Department.query.filter_by(is_active=True).order_by(Department.name_ar.asc()).all()
    role_choices = _get_role_choices()

    return render_template(
        "workflow/templates_admin/edit.html",
        t=t,
        steps=steps,
        users=users,
        departments=departments,
        role_choices=role_choices
    )


@workflow_bp.route("/templates/<int:template_id>/details")
@login_required
@perm_required("WORKFLOW_TEMPLATES_READ")
def templates_details(template_id):
    t = WorkflowTemplate.query.get_or_404(template_id)
    steps = (
        WorkflowTemplateStep.query
        .filter_by(template_id=t.id)
        .order_by(WorkflowTemplateStep.step_order.asc())
        .all()
    )
    users = User.query.order_by(User.email.asc()).all()
    users_map = {u.id: u for u in users}

    departments = Department.query.filter_by(is_active=True).order_by(Department.name_ar.asc()).all()
    depts_map = {d.id: d for d in departments}

    return render_template(
        "workflow/templates_admin/details.html",
        t=t,
        steps=steps,
        users_map=users_map,
        depts_map=depts_map
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
    role = (request.form.get("approver_role") or "").strip()
    sla_days = _to_int(request.form.get("sla_days"), default=None)

    if not kind:
        flash("يرجى اختيار نوع المعتمد (USER/ROLE/DEPARTMENT).", "danger")
        return redirect(url_for("workflow.templates_edit", template_id=t.id))

    if kind == "USER" and not user_id:
        flash("يرجى اختيار مستخدم للخطوة.", "danger")
        return redirect(url_for("workflow.templates_edit", template_id=t.id))

    if kind == "ROLE" and not role:
        flash("يرجى إدخال ROLE للخطوة.", "danger")
        return redirect(url_for("workflow.templates_edit", template_id=t.id))

    if kind == "DEPARTMENT" and not dept_id:
        flash("يرجى إدخال رقم الدائرة (department_id) للخطوة.", "danger")
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
        approver_role=role if kind == "ROLE" else None,
        sla_days=sla_days
    )
    db.session.add(step)
    db.session.commit()

    flash("تمت إضافة خطوة.", "success")
    return redirect(url_for("workflow.templates_edit", template_id=t.id))


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
    departments = Department.query.filter_by(is_active=True).order_by(Department.name_ar.asc()).all()
    role_choices = _get_role_choices()

    if request.method == "POST":
        kind = _normalize_kind(request.form.get("approver_kind"))
        user_id = _to_int(request.form.get("approver_user_id"), default=None)
        dept_id = _to_int(request.form.get("approver_department_id"), default=None)
        role = (request.form.get("approver_role") or "").strip()
        sla_days = _to_int(request.form.get("sla_days"), default=None)

        if not kind:
            flash("نوع المعتمد غير صحيح.", "danger")
            return redirect(request.url)

        if kind == "USER" and not user_id:
            flash("اختر مستخدم.", "danger")
            return redirect(request.url)

        if kind == "ROLE" and not role:
            flash("أدخل ROLE.", "danger")
            return redirect(request.url)

        if kind == "DEPARTMENT" and not dept_id:
            flash("أدخل department_id.", "danger")
            return redirect(request.url)

        s.approver_kind = kind
        s.approver_user_id = user_id if kind == "USER" else None
        s.approver_department_id = dept_id if kind == "DEPARTMENT" else None
        s.approver_role = role if kind == "ROLE" else None
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
        role_choices=role_choices
    )
