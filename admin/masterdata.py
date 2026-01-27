# admin/masterdata.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, send_file
from io import BytesIO
from datetime import datetime

from flask_login import login_required
from sqlalchemy import or_
from extensions import db
from utils.perms import perm_required
from utils.excel import make_xlsx_bytes

from models import Organization, Directorate, Department, Section, Role, User, UserPermission, RequestType, WorkflowRoutingRule, WorkflowRequest

masterdata_bp = Blueprint("masterdata", __name__, url_prefix="/admin/masterdata")

PERM_MODULES = [
    ("MASTERDATA", "البيانات الأساسية (منظمات/إدارات/دوائر)", "MASTERDATA_MANAGE"),
    ("WORKFLOW_TEMPLATES", "مسارات العمل (Templates)", "WORKFLOW_TEMPLATES_MANAGE"),
    ("REQUEST_TYPES", "أنواع الطلبات (Request Types)", "REQUEST_TYPES_MANAGE"),
    ("WORKFLOW_ROUTING", "قواعد التوجيه (Routing Rules)", "WORKFLOW_ROUTING_MANAGE"),
    ("USER_PERMISSIONS", "إدارة صلاحيات المستخدمين", "USER_PERMISSIONS_MANAGE"),
]

PERM_EXTRA_KEYS = [
    ("VIEW_DASHBOARD", "رؤية لوحة Dashboard"),
    ("VIEW_ESCALATIONS", "رؤية صفحة 🚨 Escalations"),
]

PERM_ACTIONS = [
    ("READ", "قراءة"),
    ("CREATE", "إنشاء"),
    ("UPDATE", "تعديل"),
    ("DELETE", "حذف"),
]


def _clean(s): return (s or "").strip()



def _apply_sort(query, model, allowed: list[str], default_sort: str = "name_ar"):
    sort = (request.args.get("sort") or default_sort).strip()
    direction = (request.args.get("dir") or "asc").strip().lower()

    if sort not in allowed:
        sort = default_sort
    if direction not in ("asc", "desc"):
        direction = "asc"

    col = getattr(model, sort, None)
    if col is None:
        sort = default_sort
        col = getattr(model, sort)

    query = query.order_by(col.desc() if direction == "desc" else col.asc())
    return query, sort, direction


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
    q = request.args.get("q", "").strip()
    query = Organization.query

    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            Organization.name_ar.ilike(like),
            Organization.name_en.ilike(like),
            Organization.code.ilike(like)
        ))

    query, sort, direction = _apply_sort(
        query,
        Organization,
        allowed=["id", "name_ar", "name_en", "code", "is_active"],
        default_sort="name_ar"
    )

    items = query.all()
    return render_template("admin/masterdata/org_list.html", items=items, q=q, sort=sort, direction=direction)



@masterdata_bp.route("/organizations/export.xlsx")
@login_required
@perm_required("MASTERDATA_READ")
def org_export_excel():
    q = request.args.get("q", "").strip()
    query = Organization.query
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            Organization.name_ar.ilike(like),
            Organization.name_en.ilike(like),
            Organization.code.ilike(like)
        ))

    query, sort, direction = _apply_sort(
        query,
        Organization,
        allowed=["id", "name_ar", "name_en", "code", "is_active"],
        default_sort="name_ar"
    )

    items = query.all()
    headers = ["ID", "الاسم (AR)", "الاسم (EN)", "Code", "نشط"]
    rows = []
    for o in items:
        rows.append([
            o.id,
            o.name_ar,
            o.name_en,
            o.code,
            "نعم" if o.is_active else "لا",
        ])

    data = make_xlsx_bytes("Organizations", headers, rows)
    filename = f"organizations_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.xlsx"
    return send_file(
        BytesIO(data),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )

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


@masterdata_bp.route("/organizations/<int:org_id>/delete", methods=["POST"])
@login_required
@perm_required("MASTERDATA_DELETE")
def org_delete(org_id):
    o = Organization.query.get_or_404(org_id)
    try:
        has_children = Directorate.query.filter_by(organization_id=o.id).first() is not None
        if has_children:
            o.is_active = False
            db.session.commit()
            flash("لا يمكن حذف المنظمة لوجود إدارات مرتبطة بها، تم تعطيلها بدلاً من ذلك.", "warning")
        else:
            db.session.delete(o)
            db.session.commit()
            flash("تم حذف المنظمة.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"تعذر حذف المنظمة: {e}", "danger")
    return redirect(url_for("masterdata.org_list"))


# -------------------------
# Directorates
# -------------------------
@masterdata_bp.route("/directorates")
@login_required
@perm_required("MASTERDATA_READ")
def dir_list():
    q = request.args.get("q", "").strip()
    query = Directorate.query

    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            Directorate.name_ar.ilike(like),
            Directorate.name_en.ilike(like)
        ))

    query, sort, direction = _apply_sort(
        query,
        Directorate,
        allowed=["id", "name_ar", "name_en", "code", "organization_id", "is_active"],
        default_sort="name_ar"
    )
    items = query.all()

    orgs = Organization.query.order_by(Organization.id.asc()).all()
    org_map = {o.id: o for o in orgs}
    return render_template(
        "admin/masterdata/dir_list.html",
        items=items,
        q=q,
        org_map=org_map,
        sort=sort,
        direction=direction,
    )



@masterdata_bp.route("/directorates/export.xlsx")
@login_required
@perm_required("MASTERDATA_READ")
def dir_export_excel():
    q = request.args.get("q", "").strip()
    query = Directorate.query
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            Directorate.name_ar.ilike(like),
            Directorate.name_en.ilike(like),
            Directorate.code.ilike(like),
        ))

    query, sort, direction = _apply_sort(
        query,
        Directorate,
        allowed=["id", "name_ar", "name_en", "code", "organization_id", "is_active"],
        default_sort="name_ar"
    )

    items = query.all()
    orgs = Organization.query.order_by(Organization.id.asc()).all()
    org_map = {o.id: o for o in orgs}

    headers = ["ID", "المنظمة", "الاسم (AR)", "الاسم (EN)", "Code", "نشط"]
    rows = []
    for d in items:
        rows.append([
            d.id,
            (org_map.get(d.organization_id).name_ar if org_map.get(d.organization_id) else d.organization_id),
            d.name_ar,
            d.name_en,
            d.code,
            "نعم" if d.is_active else "لا",
        ])

    data = make_xlsx_bytes("Directorates", headers, rows)
    filename = f"directorates_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.xlsx"
    from io import BytesIO
    return send_file(
        BytesIO(data),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )

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


@masterdata_bp.route("/directorates/<int:dir_id>/delete", methods=["POST"])
@login_required
@perm_required("MASTERDATA_DELETE")
def dir_delete(dir_id):
    d = Directorate.query.get_or_404(dir_id)
    try:
        has_children = Department.query.filter_by(directorate_id=d.id).first() is not None
        if has_children:
            d.is_active = False
            db.session.commit()
            flash("لا يمكن حذف الإدارة لوجود دوائر مرتبطة بها، تم تعطيلها بدلاً من ذلك.", "warning")
        else:
            db.session.delete(d)
            db.session.commit()
            flash("تم حذف الإدارة.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"تعذر حذف الإدارة: {e}", "danger")
    return redirect(url_for("masterdata.dir_list"))

# -------------------------
# Departments
# -------------------------
@masterdata_bp.route("/departments")
@login_required
@perm_required("MASTERDATA_READ")
def dept_list():
    q = request.args.get("q", "").strip()
    query = Department.query

    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            Department.name_ar.ilike(like),
            Department.name_en.ilike(like)
        ))

    query, sort, direction = _apply_sort(
        query,
        Department,
        allowed=["id", "name_ar", "name_en", "code", "directorate_id", "is_active"],
        default_sort="name_ar"
    )
    items = query.all()

    dirs = Directorate.query.order_by(Directorate.id.asc()).all()
    dir_map = {d.id: d for d in dirs}
    return render_template(
        "admin/masterdata/dept_list.html",
        items=items,
        q=q,
        dir_map=dir_map,
        sort=sort,
        direction=direction,
    )



@masterdata_bp.route("/departments/export.xlsx")
@login_required
@perm_required("MASTERDATA_READ")
def dept_export_excel():
    q = request.args.get("q", "").strip()
    query = Department.query
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            Department.name_ar.ilike(like),
            Department.name_en.ilike(like),
            Department.code.ilike(like),
        ))

    query, sort, direction = _apply_sort(
        query,
        Department,
        allowed=["id", "name_ar", "name_en", "code", "directorate_id", "is_active"],
        default_sort="name_ar"
    )

    items = query.all()
    dirs = Directorate.query.order_by(Directorate.id.asc()).all()
    dir_map = {d.id: d for d in dirs}

    headers = ["ID", "الإدارة", "الدائرة (AR)", "الدائرة (EN)", "Code", "نشط"]
    rows = []
    for dep in items:
        dir_obj = dir_map.get(dep.directorate_id)
        rows.append([
            dep.id,
            (dir_obj.name_ar if dir_obj else dep.directorate_id),
            dep.name_ar,
            dep.name_en,
            dep.code,
            "نعم" if dep.is_active else "لا",
        ])

    data = make_xlsx_bytes("Departments", headers, rows)
    filename = f"departments_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.xlsx"
    return send_file(
        BytesIO(data),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )

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


@masterdata_bp.route("/departments/<int:dept_id>/delete", methods=["POST"])
@login_required
@perm_required("MASTERDATA_DELETE")
def dept_delete(dept_id):
    dep = Department.query.get_or_404(dept_id)
    try:
        has_children = Section.query.filter_by(department_id=dep.id).first() is not None
        if has_children:
            dep.is_active = False
            db.session.commit()
            flash("لا يمكن حذف الدائرة لوجود أقسام مرتبطة بها، تم تعطيلها بدلاً من ذلك.", "warning")
        else:
            db.session.delete(dep)
            db.session.commit()
            flash("تم حذف الدائرة.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"تعذر حذف الدائرة: {e}", "danger")
    return redirect(url_for("masterdata.dept_list"))


# -------------------------
# Sections (أقسام)
# -------------------------
@masterdata_bp.route("/sections")
@login_required
@perm_required("MASTERDATA_READ")
def sections_list():
    q = request.args.get("q", "").strip()
    query = Section.query

    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            Section.name_ar.ilike(like),
            Section.name_en.ilike(like),
            Section.code.ilike(like),
        ))

    query, sort, direction = _apply_sort(
        query,
        Section,
        allowed=["id", "name_ar", "name_en", "code", "department_id", "is_active"],
        default_sort="name_ar"
    )
    items = query.all()

    # Build mapping for department + directorate names
    departments = Department.query.order_by(Department.id.asc()).all()
    dept_map = {d.id: d for d in departments}
    dirs = Directorate.query.order_by(Directorate.id.asc()).all()
    dir_map = {d.id: d for d in dirs}
    return render_template(
        "admin/masterdata/sections_list.html",
        items=items,
        q=q,
        dept_map=dept_map,
        dir_map=dir_map,
        sort=sort,
        direction=direction,
    )




@masterdata_bp.route("/sections/export.xlsx")
@login_required
@perm_required("MASTERDATA_READ")
def sections_export_excel():
    q = request.args.get("q", "").strip()
    query = Section.query
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            Section.name_ar.ilike(like),
            Section.name_en.ilike(like),
            Section.code.ilike(like),
        ))

    query, sort, direction = _apply_sort(
        query,
        Section,
        allowed=["id", "name_ar", "name_en", "code", "department_id", "is_active"],
        default_sort="name_ar"
    )

    items = query.all()
    departments = Department.query.order_by(Department.id.asc()).all()
    dept_map = {d.id: d for d in departments}
    dirs = Directorate.query.order_by(Directorate.id.asc()).all()
    dir_map = {d.id: d for d in dirs}

    headers = ["ID", "الإدارة", "الدائرة", "القسم (AR)", "القسم (EN)", "Code", "نشط"]
    rows = []
    for s in items:
        dep = dept_map.get(s.department_id)
        dir_obj = dir_map.get(dep.directorate_id) if dep else None
        rows.append([
            s.id,
            (dir_obj.name_ar if dir_obj else (dep.directorate_id if dep else "-")),
            (dep.name_ar if dep else s.department_id),
            s.name_ar,
            s.name_en,
            s.code,
            "نعم" if s.is_active else "لا",
        ])

    data = make_xlsx_bytes("Sections", headers, rows)
    filename = f"sections_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.xlsx"
    return send_file(
        BytesIO(data),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )

@masterdata_bp.route("/sections/new", methods=["GET", "POST"])
@login_required
@perm_required("MASTERDATA_CREATE")
def sections_new():
    departments = Department.query.order_by(Department.name_ar.asc()).all()

    if request.method == "POST":
        dept_id = request.form.get("department_id")
        name_ar = _clean(request.form.get("name_ar"))
        name_en = _clean(request.form.get("name_en")) or None
        code = _clean(request.form.get("code")) or None
        is_active = (request.form.get("is_active") == "1")

        if not (dept_id or "").isdigit():
            flash("اختر دائرة.", "danger")
            return redirect(request.url)
        if not name_ar:
            flash("اسم القسم (عربي) مطلوب.", "danger")
            return redirect(request.url)

        s = Section(
            department_id=int(dept_id),
            name_ar=name_ar,
            name_en=name_en,
            code=code,
            is_active=is_active,
        )
        db.session.add(s)
        db.session.commit()
        flash("تم إنشاء القسم.", "success")
        return redirect(url_for("masterdata.sections_list"))

    return render_template("admin/masterdata/sections_form.html", s=None, departments=departments)


@masterdata_bp.route("/sections/<int:section_id>/edit", methods=["GET", "POST"])
@login_required
@perm_required("MASTERDATA_UPDATE")
def sections_edit(section_id):
    s = Section.query.get_or_404(section_id)
    departments = Department.query.order_by(Department.name_ar.asc()).all()

    if request.method == "POST":
        dept_id = request.form.get("department_id")
        name_ar = _clean(request.form.get("name_ar"))
        name_en = _clean(request.form.get("name_en")) or None
        code = _clean(request.form.get("code")) or None
        is_active = (request.form.get("is_active") == "1")

        if not (dept_id or "").isdigit():
            flash("اختر دائرة.", "danger")
            return redirect(request.url)
        if not name_ar:
            flash("اسم القسم (عربي) مطلوب.", "danger")
            return redirect(request.url)

        s.department_id = int(dept_id)
        s.name_ar = name_ar
        s.name_en = name_en
        s.code = code
        s.is_active = is_active

        db.session.commit()
        flash("تم حفظ التعديلات.", "success")
        return redirect(url_for("masterdata.sections_list"))

    return render_template("admin/masterdata/sections_form.html", s=s, departments=departments)


@masterdata_bp.route("/sections/<int:section_id>/delete", methods=["POST"])
@login_required
@perm_required("MASTERDATA_DELETE")
def sections_delete(section_id):
    s = Section.query.get_or_404(section_id)
    try:
        db.session.delete(s)
        db.session.commit()
        flash("تم حذف القسم.", "warning")
    except Exception as e:
        db.session.rollback()
        flash(f"تعذر حذف القسم: {e}", "danger")
    return redirect(url_for("masterdata.sections_list"))

# -------------------------
# Request Types
# -------------------------
@masterdata_bp.route("/request-types")
@login_required
@perm_required("REQUEST_TYPES_READ")
def request_types_list():
    q = request.args.get("q", "").strip()
    query = RequestType.query

    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            RequestType.name_ar.ilike(like),
            RequestType.name_en.ilike(like),
            RequestType.code.ilike(like)
        ))

    items = query.order_by(RequestType.id.asc()).all()
    return render_template("admin/masterdata/request_types_list.html", items=items, q=q)

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
# -------------------------
# Permissions management
# -------------------------
@masterdata_bp.route("/permissions", methods=["GET", "POST"])
@login_required
@perm_required("USER_PERMISSIONS_UPDATE")
def permissions_manage():
    users = User.query.order_by(User.id.asc()).all()
    if not _current_is_super_admin():
        users = [u for u in users if not _is_super_admin_user(u)]

    selected_user_id = request.args.get("user_id")

    action_codes = [a for a, _ in PERM_ACTIONS]  # e.g. ["CREATE","READ","UPDATE","DELETE"]

    # Build managed keys list for cleanup (CRUD + legacy *_MANAGE + extra)
    managed_keys = set()
    for prefix, _label, legacy in PERM_MODULES:
        for act in action_codes:
            managed_keys.add(f"{prefix}_{act}")
        if legacy:
            managed_keys.add(legacy)
    for k, _lbl in PERM_EXTRA_KEYS:
        managed_keys.add(k)

    if request.method == "POST":
        uid = (request.form.get("user_id") or "").strip()
        if not uid.isdigit():
            flash("يرجى اختيار مستخدم صحيح.", "danger")
            return redirect(url_for("masterdata.permissions_manage"))

        uid = int(uid)
        target = User.query.get(uid)
        if not target:
            flash("المستخدم غير موجود.", "danger")
            return redirect(url_for("masterdata.permissions_manage"))

        # HARD BLOCK: only SUPER_ADMIN can edit SUPER_ADMIN permissions
        if _is_super_admin_user(target) and not _current_is_super_admin():
            flash("لا يمكنك تعديل صلاحيات حساب SUPER_ADMIN.", "danger")
            return redirect(url_for("masterdata.permissions_manage"))

        # Remove old perms for managed keys
        UserPermission.query.filter(
            UserPermission.user_id == uid,
            UserPermission.key.in_(list(managed_keys))
        ).delete(synchronize_session=False)

        # Add selected CRUD perms
        for prefix, _label, _legacy in PERM_MODULES:
            for act in action_codes:
                if request.form.get(f"perm_{prefix}_{act}") == "1":
                    k = f"{prefix}_{act}"
                    db.session.add(UserPermission(user_id=uid, key=k, is_allowed=True))

        # Add selected EXTRA perms
        for k, _lbl in PERM_EXTRA_KEYS:
            if request.form.get(f"perm_extra_{k}") == "1":
                db.session.add(UserPermission(user_id=uid, key=k, is_allowed=True))

        db.session.commit()
        flash("تم حفظ صلاحيات المستخدم.", "success")
        return redirect(url_for("masterdata.permissions_manage", user_id=uid))

    # GET view
    selected = None
    checked_keys = set()
    current_keys = set()

    if selected_user_id and str(selected_user_id).isdigit():
        uid = int(selected_user_id)
        selected = User.query.get(uid)
        if selected:
            if _is_super_admin_user(selected) and not _current_is_super_admin():
                flash("لا يمكنك عرض/تعديل صلاحيات حساب SUPER_ADMIN.", "warning")
                return redirect(url_for("masterdata.permissions_manage"))

            rows = UserPermission.query.filter_by(user_id=uid).all()
            current_keys = {
                (p.key or "").strip().upper()
                for p in rows
                if p.is_allowed
            }

            # CRUD keys (+ legacy mapping)
            for prefix, _label, legacy in PERM_MODULES:
                # legacy -> all CRUD enabled
                if legacy and legacy.upper() in current_keys:
                    for act in action_codes:
                        checked_keys.add(f"{prefix}_{act}")
                    checked_keys.add(legacy)
                    continue

                for act in action_codes:
                    k = f"{prefix}_{act}".upper()
                    if k in current_keys:
                        checked_keys.add(f"{prefix}_{act}")

                if legacy and legacy.upper() in current_keys:
                    checked_keys.add(legacy)

            # Extra keys
            for k, _lbl in PERM_EXTRA_KEYS:
                if k.upper() in current_keys:
                    checked_keys.add(k)

    return render_template(
        "admin/masterdata/permissions.html",
        users=users,
        selected=selected,
        perm_modules=PERM_MODULES,
        perm_actions=PERM_ACTIONS,
        checked_keys=checked_keys,
        current_keys=current_keys,
        extra_keys=PERM_EXTRA_KEYS,
    )
@masterdata_bp.route("/roles")
@login_required
@perm_required("MASTERDATA_READ")
def roles_list():
    q = request.args.get("q", "").strip()
    query = Role.query

    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            Role.code.ilike(like),
            Role.name_ar.ilike(like),
            Role.name_en.ilike(like)
        ))

    items = query.order_by(Role.id.asc()).all()
    return render_template("admin/masterdata/roles_list.html", items=items, q=q)


@masterdata_bp.route("/roles/new", methods=["GET", "POST"])
@login_required
@perm_required("MASTERDATA_CREATE")
def roles_new():
    if request.method == "POST":
        code = _clean(request.form.get("code"))
        name_ar = _clean(request.form.get("name_ar"))
        name_en = _clean(request.form.get("name_en"))
        is_active = (request.form.get("is_active") == "1")

        if not code:
            flash("كود الدور مطلوب.", "danger")
            return redirect(request.url)

        code = code.replace(" ", "_")
        if Role.query.filter_by(code=code).first():
            flash("هذا الدور موجود مسبقًا.", "danger")
            return redirect(request.url)

        r = Role(code=code, name_ar=name_ar or None, name_en=name_en or None, is_active=is_active)
        db.session.add(r)
        db.session.commit()
        flash("تم إنشاء الدور.", "success")
        return redirect(url_for("masterdata.roles_list"))

    return render_template("admin/masterdata/roles_form.html", r=None)


@masterdata_bp.route("/roles/<int:role_id>/edit", methods=["GET", "POST"])
@login_required
@perm_required("MASTERDATA_UPDATE")
def roles_edit(role_id):
    r = Role.query.get_or_404(role_id)

    if request.method == "POST":
        code = _clean(request.form.get("code"))
        name_ar = _clean(request.form.get("name_ar"))
        name_en = _clean(request.form.get("name_en"))
        is_active = (request.form.get("is_active") == "1")

        if not code:
            flash("كود الدور مطلوب.", "danger")
            return redirect(request.url)

        code = code.replace(" ", "_")
        exists = Role.query.filter(Role.code == code, Role.id != r.id).first()
        if exists:
            flash("يوجد دور آخر بنفس الكود.", "danger")
            return redirect(request.url)

        r.code = code
        r.name_ar = name_ar or None
        r.name_en = name_en or None
        r.is_active = is_active

        db.session.commit()
        flash("تم حفظ الدور.", "success")
        return redirect(url_for("masterdata.roles_list"))

    return render_template("admin/masterdata/roles_form.html", r=r)


@masterdata_bp.route("/roles/<int:role_id>/delete", methods=["POST"])
@login_required
@perm_required("MASTERDATA_DELETE")
def roles_delete(role_id):
    r = Role.query.get_or_404(role_id)
    try:
        used = User.query.filter(User.role.isnot(None)).filter(User.role.ilike(r.code)).count() > 0
    except Exception:
        used = False

    if used:
        r.is_active = False
        db.session.commit()
        flash("لا يمكن حذف الدور لأنه مستخدم لدى مستخدمين. تم تعطيله بدلاً من ذلك.", "warning")
    else:
        db.session.delete(r)
        db.session.commit()
        flash("تم حذف الدور.", "warning")
    return redirect(url_for("masterdata.roles_list"))
