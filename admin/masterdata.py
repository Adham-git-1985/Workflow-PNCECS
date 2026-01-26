# admin/masterdata.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from flask_login import login_required
from extensions import db
from utils.perms import perm_required

from models import Organization, Directorate, Department, User, UserPermission, RequestType, WorkflowRoutingRule, WorkflowRequest

masterdata_bp = Blueprint("masterdata", __name__, url_prefix="/admin/masterdata")

PERM_MODULES = [
    ("MASTERDATA", "البيانات الأساسية (منظمات/إدارات/دوائر)", "MASTERDATA_MANAGE"),
    ("WORKFLOW_TEMPLATES", "مسارات العمل (Templates)", "WORKFLOW_TEMPLATES_MANAGE"),
    ("REQUEST_TYPES", "أنواع الطلبات (Request Types)", "REQUEST_TYPES_MANAGE"),
    ("WORKFLOW_ROUTING", "قواعد التوجيه (Routing Rules)", "WORKFLOW_ROUTING_MANAGE"),
    ("USER_PERMISSIONS", "إدارة صلاحيات المستخدمين", "USER_PERMISSIONS_MANAGE"),
]

PERM_ACTIONS = [
    ("READ", "قراءة"),
    ("CREATE", "إنشاء"),
    ("UPDATE", "تعديل"),
    ("DELETE", "حذف"),
]


def _clean(s): return (s or "").strip()


def _is_super_admin_user(u: User) -> bool:
    return ((getattr(u, "role", "") or "").strip().upper() == "SUPER_ADMIN")


def _current_is_super_admin() -> bool:
    # keep it local to avoid import cycles
    from flask_login import current_user
    return ((getattr(current_user, "role", "") or "").strip().upper() == "SUPER_ADMIN")

# -------------------------
# Organizations
# -------------------------
@masterdata_bp.route("/organizations")
@login_required
@perm_required("MASTERDATA_READ")
def org_list():
    items = Organization.query.order_by(Organization.id.desc()).all()
    return render_template("admin/masterdata/org_list.html", items=items)

@masterdata_bp.route("/organizations/new", methods=["GET", "POST"])
@login_required
@perm_required("MASTERDATA_CREATE")
def org_new():
    if request.method == "POST":
        name_ar = _clean(request.form.get("name_ar"))
        if not name_ar:
            flash("اسم المنظمة (عربي) مطلوب.", "danger")
            return redirect(request.url)

        o = Organization(
            name_ar=name_ar,
            name_en=_clean(request.form.get("name_en")),
            code=_clean(request.form.get("code")) or None,
            is_active=(request.form.get("is_active") == "1"),
        )
        db.session.add(o)
        db.session.commit()
        flash("تم إنشاء المنظمة.", "success")
        return redirect(url_for("masterdata.org_list"))

    return render_template("admin/masterdata/org_form.html", o=None)

@masterdata_bp.route("/organizations/<int:org_id>/edit", methods=["GET", "POST"])
@login_required
@perm_required("MASTERDATA_UPDATE")
def org_edit(org_id):
    o = Organization.query.get_or_404(org_id)
    if request.method == "POST":
        name_ar = _clean(request.form.get("name_ar"))
        if not name_ar:
            flash("اسم المنظمة (عربي) مطلوب.", "danger")
            return redirect(request.url)

        o.name_ar = name_ar
        o.name_en = _clean(request.form.get("name_en"))
        o.code = _clean(request.form.get("code")) or None
        o.is_active = (request.form.get("is_active") == "1")

        db.session.commit()
        flash("تم حفظ التعديلات.", "success")
        return redirect(url_for("masterdata.org_list"))

    return render_template("admin/masterdata/org_form.html", o=o)

# -------------------------
# Directorates
# -------------------------
@masterdata_bp.route("/directorates")
@login_required
@perm_required("MASTERDATA_READ")
def dir_list():
    items = (
        Directorate.query
        .order_by(Directorate.id.desc())
        .all()
    )
    orgs = Organization.query.order_by(Organization.name_ar.asc()).all()
    org_map = {o.id: o for o in orgs}
    return render_template("admin/masterdata/dir_list.html", items=items, org_map=org_map)

@masterdata_bp.route("/directorates/new", methods=["GET", "POST"])
@login_required
@perm_required("MASTERDATA_CREATE")
def dir_new():
    orgs = Organization.query.order_by(Organization.name_ar.asc()).all()

    if request.method == "POST":
        org_id = request.form.get("organization_id")
        name_ar = _clean(request.form.get("name_ar"))

        if not (org_id or "").isdigit():
            flash("اختر منظمة.", "danger")
            return redirect(request.url)
        if not name_ar:
            flash("اسم الإدارة (عربي) مطلوب.", "danger")
            return redirect(request.url)

        d = Directorate(
            organization_id=int(org_id),
            name_ar=name_ar,
            name_en=_clean(request.form.get("name_en")),
            code=_clean(request.form.get("code")) or None,
            is_active=(request.form.get("is_active") == "1"),
        )
        db.session.add(d)
        db.session.commit()
        flash("تم إنشاء الإدارة.", "success")
        return redirect(url_for("masterdata.dir_list"))

    return render_template("admin/masterdata/dir_form.html", d=None, orgs=orgs)

@masterdata_bp.route("/directorates/<int:dir_id>/edit", methods=["GET", "POST"])
@login_required
@perm_required("MASTERDATA_UPDATE")
def dir_edit(dir_id):
    d = Directorate.query.get_or_404(dir_id)
    orgs = Organization.query.order_by(Organization.name_ar.asc()).all()

    if request.method == "POST":
        org_id = request.form.get("organization_id")
        name_ar = _clean(request.form.get("name_ar"))

        if not (org_id or "").isdigit():
            flash("اختر منظمة.", "danger")
            return redirect(request.url)
        if not name_ar:
            flash("اسم الإدارة (عربي) مطلوب.", "danger")
            return redirect(request.url)

        d.organization_id = int(org_id)
        d.name_ar = name_ar
        d.name_en = _clean(request.form.get("name_en"))
        d.code = _clean(request.form.get("code")) or None
        d.is_active = (request.form.get("is_active") == "1")

        db.session.commit()
        flash("تم حفظ التعديلات.", "success")
        return redirect(url_for("masterdata.dir_list"))

    return render_template("admin/masterdata/dir_form.html", d=d, orgs=orgs)

# -------------------------
# Departments
# -------------------------
@masterdata_bp.route("/departments")
@login_required
@perm_required("MASTERDATA_READ")
def dept_list():
    items = Department.query.order_by(Department.id.desc()).all()
    dirs = Directorate.query.order_by(Directorate.name_ar.asc()).all()
    dir_map = {d.id: d for d in dirs}
    return render_template("admin/masterdata/dept_list.html", items=items, dir_map=dir_map)

@masterdata_bp.route("/departments/new", methods=["GET", "POST"])
@login_required
@perm_required("MASTERDATA_CREATE")
def dept_new():
    dirs = Directorate.query.order_by(Directorate.name_ar.asc()).all()

    if request.method == "POST":
        dir_id = request.form.get("directorate_id")
        name_ar = _clean(request.form.get("name_ar"))

        if not (dir_id or "").isdigit():
            flash("اختر إدارة.", "danger")
            return redirect(request.url)
        if not name_ar:
            flash("اسم الدائرة (عربي) مطلوب.", "danger")
            return redirect(request.url)

        dep = Department(
            directorate_id=int(dir_id),
            name_ar=name_ar,
            name_en=_clean(request.form.get("name_en")),
            code=_clean(request.form.get("code")) or None,
            is_active=(request.form.get("is_active") == "1"),
        )
        db.session.add(dep)
        db.session.commit()
        flash("تم إنشاء الدائرة.", "success")
        return redirect(url_for("masterdata.dept_list"))

    return render_template("admin/masterdata/dept_form.html", dep=None, dirs=dirs)

@masterdata_bp.route("/departments/<int:dept_id>/edit", methods=["GET", "POST"])
@login_required
@perm_required("MASTERDATA_UPDATE")
def dept_edit(dept_id):
    dep = Department.query.get_or_404(dept_id)
    dirs = Directorate.query.order_by(Directorate.name_ar.asc()).all()

    if request.method == "POST":
        dir_id = request.form.get("directorate_id")
        name_ar = _clean(request.form.get("name_ar"))

        if not (dir_id or "").isdigit():
            flash("اختر إدارة.", "danger")
            return redirect(request.url)
        if not name_ar:
            flash("اسم الدائرة (عربي) مطلوب.", "danger")
            return redirect(request.url)

        dep.directorate_id = int(dir_id)
        dep.name_ar = name_ar
        dep.name_en = _clean(request.form.get("name_en"))
        dep.code = _clean(request.form.get("code")) or None
        dep.is_active = (request.form.get("is_active") == "1")

        db.session.commit()
        flash("تم حفظ التعديلات.", "success")
        return redirect(url_for("masterdata.dept_list"))

    return render_template("admin/masterdata/dept_form.html", dep=dep, dirs=dirs)

# -------------------------
# Request Types
# -------------------------
@masterdata_bp.route("/request-types")
@login_required
@perm_required("REQUEST_TYPES_READ")
def request_types_list():
    items = RequestType.query.order_by(RequestType.id.desc()).all()
    return render_template("admin/masterdata/request_types_list.html", items=items)

@masterdata_bp.route("/request-types/new", methods=["GET", "POST"])
@login_required
@perm_required("REQUEST_TYPES_CREATE")
def request_types_new():
    if request.method == "POST":
        code = _clean(request.form.get("code")).upper()
        name_ar = _clean(request.form.get("name_ar"))
        name_en = _clean(request.form.get("name_en")) or None
        is_active = (request.form.get("is_active") == "1")

        if not code:
            flash("الكود (CODE) مطلوب.", "danger")
            return redirect(request.url)
        if not name_ar:
            flash("الاسم العربي مطلوب.", "danger")
            return redirect(request.url)

        exists = RequestType.query.filter_by(code=code).first()
        if exists:
            flash("هذا الكود مستخدم مسبقاً.", "danger")
            return redirect(request.url)

        rt = RequestType(code=code, name_ar=name_ar, name_en=name_en, is_active=is_active)
        db.session.add(rt)
        db.session.commit()
        flash("تم إنشاء نوع الطلب.", "success")
        return redirect(url_for("masterdata.request_types_list"))

    return render_template("admin/masterdata/request_types_form.html", rt=None)

@masterdata_bp.route("/request-types/<int:rt_id>/edit", methods=["GET", "POST"])
@login_required
@perm_required("REQUEST_TYPES_UPDATE")
def request_types_edit(rt_id):
    rt = RequestType.query.get_or_404(rt_id)

    if request.method == "POST":
        code = _clean(request.form.get("code")).upper()
        name_ar = _clean(request.form.get("name_ar"))
        name_en = _clean(request.form.get("name_en")) or None
        is_active = (request.form.get("is_active") == "1")

        if not code:
            flash("الكود (CODE) مطلوب.", "danger")
            return redirect(request.url)
        if not name_ar:
            flash("الاسم العربي مطلوب.", "danger")
            return redirect(request.url)

        other = RequestType.query.filter(RequestType.code == code, RequestType.id != rt.id).first()
        if other:
            flash("هذا الكود مستخدم مسبقاً.", "danger")
            return redirect(request.url)

        rt.code = code
        rt.name_ar = name_ar
        rt.name_en = name_en
        rt.is_active = is_active

        db.session.commit()
        flash("تم حفظ التعديلات.", "success")
        return redirect(url_for("masterdata.request_types_list"))

    return render_template("admin/masterdata/request_types_form.html", rt=rt)

@masterdata_bp.route("/request-types/<int:rt_id>/delete", methods=["POST"])
@login_required
@perm_required("REQUEST_TYPES_DELETE")
def request_types_delete(rt_id):
    rt = RequestType.query.get_or_404(rt_id)

    # prevent delete if used
    used_in_req = WorkflowRequest.query.filter(WorkflowRequest.request_type_id == rt.id).count()
    used_in_rules = WorkflowRoutingRule.query.filter(WorkflowRoutingRule.request_type_id == rt.id).count()

    if used_in_req or used_in_rules:
        # safer: deactivate
        rt.is_active = False
        db.session.commit()
        flash("هذا النوع مستخدم بالفعل. تم تعطيله بدلاً من الحذف.", "warning")
        return redirect(url_for("masterdata.request_types_list"))

    db.session.delete(rt)
    db.session.commit()
    flash("تم حذف نوع الطلب.", "warning")
    return redirect(url_for("masterdata.request_types_list"))

# -------------------------
# Permissions management
# -------------------------
@masterdata_bp.route("/permissions", methods=["GET", "POST"])
@login_required
@perm_required("USER_PERMISSIONS_UPDATE")
def permissions_manage():
    users = User.query.order_by(User.email.asc()).all()
    # Admin (or any non-super-admin) must NOT be able to manage SUPER_ADMIN permissions
    if not _current_is_super_admin():
        users = [u for u in users if not _is_super_admin_user(u)]
    selected_user_id = request.args.get("user_id")

    # build all managed keys (CRUD + legacy *_MANAGE) to keep things clean
    action_codes = [a for a, _ in PERM_ACTIONS]
    crud_keys = [f"{prefix}_{a}" for (prefix, _label, _legacy) in PERM_MODULES for a in action_codes]
    legacy_keys = [legacy for (_prefix, _label, legacy) in PERM_MODULES if legacy]
    all_keys = sorted(set(crud_keys + legacy_keys))

    if request.method == "POST":
        uid = request.form.get("user_id")
        if not (uid or "").isdigit():
            flash("اختر مستخدم.", "danger")
            return redirect(request.url)

        uid = int(uid)

        target = User.query.get(uid)
        if not target:
            flash("المستخدم غير موجود.", "danger")
            return redirect(request.url)

        # HARD BLOCK: no one except SUPER_ADMIN can modify SUPER_ADMIN permissions
        if _is_super_admin_user(target) and not _current_is_super_admin():
            flash("لا يمكنك تعديل صلاحيات حساب SUPER_ADMIN.", "danger")
            return redirect(url_for("masterdata.permissions_manage"))

        # remove old perms for these keys
        UserPermission.query.filter(
            UserPermission.user_id == uid,
            UserPermission.key.in_(all_keys)
        ).delete(synchronize_session=False)

        # add checked perms (CRUD only)
        for (prefix, _label, _legacy) in PERM_MODULES:
            for act in action_codes:
                k = f"{prefix}_{act}"
                if request.form.get(f"perm_{prefix}_{act}") == "1":
                    db.session.add(UserPermission(user_id=uid, key=k, is_allowed=True))

        db.session.commit()
        flash("تم حفظ صلاحيات المستخدم.", "success")
        return redirect(url_for("masterdata.permissions_manage", user_id=uid))

    selected = None
    checked_keys = set()
    current_keys = set()

    if (selected_user_id or "").isdigit():
        selected = User.query.get(int(selected_user_id))
        if selected:
            if _is_super_admin_user(selected) and not _current_is_super_admin():
                flash("لا يمكنك عرض/تعديل صلاحيات حساب SUPER_ADMIN.", "warning")
                return redirect(url_for("masterdata.permissions_manage"))
            rows = UserPermission.query.filter_by(user_id=selected.id).all()
            current_keys = {
                (p.key or "").strip().upper()
                for p in rows
                if p.is_allowed
            }

            # If legacy MANAGE exists, treat CRUD as checked for that module
            for (prefix, _label, legacy) in PERM_MODULES:
                legacy_u = (legacy or "").strip().upper()
                if legacy_u and legacy_u in current_keys:
                    for act in action_codes:
                        checked_keys.add(f"{prefix}_{act}")

                for act in action_codes:
                    k = f"{prefix}_{act}"
                    if k in current_keys:
                        checked_keys.add(k)

    return render_template(
        "admin/masterdata/permissions.html",
        users=users,
        selected=selected,
        perm_modules=PERM_MODULES,
        perm_actions=PERM_ACTIONS,
        checked_keys=checked_keys,
        current_keys=current_keys,
    )
