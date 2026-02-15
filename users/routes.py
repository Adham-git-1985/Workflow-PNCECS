from flask import render_template, request, redirect, url_for, flash, abort, current_app, send_file, send_from_directory, jsonify
from flask_login import login_required, current_user
from werkzeug.security import generate_password_hash
from werkzeug.utils import secure_filename

from . import users_bp
from extensions import db
from permissions import roles_required
from models import User, AuditLog, Department, Directorate, Unit, Section, Division, Organization, Role, EmployeeFile, EmployeeAttachment
from utils.events import emit_event

from utils import system_search

# SQLAlchemy helpers
from sqlalchemy import or_

from io import BytesIO
from pathlib import Path

from utils.excel import make_xlsx_bytes

import os
import time
from datetime import datetime

AVATAR_ALLOWED_EXTS = {"png", "jpg", "jpeg", "gif", "webp"}


# ==============================
# المساعدة / الأدلة
# ==============================


@users_bp.route('/search', methods=['GET'])
@login_required
def system_search_page():
    """بحث على مستوى النظام (مسار + البوابة الإدارية + الأدلة)."""
    q = (request.args.get('q') or '').strip()
    results = []
    try:
        results = system_search.search(current_user, q, limit=50)
    except Exception:
        results = []
    return render_template('search/system_search.html', q=q, results=results)


@users_bp.route('/api/search', methods=['GET'])
@login_required
def system_search_api():
    """AJAX: returns JSON search results for the global search modal."""
    q = (request.args.get('q') or '').strip()
    try:
        results = system_search.search(current_user, q, limit=20)
    except Exception:
        results = []
    return jsonify({'q': q, 'results': results})


@users_bp.route('/help/system', methods=['GET'])
@login_required
def help_system_master_guide():
    """الدليل الشامل للنظام (مسار + البوابة الإدارية) — مناسب للمستخدم لأول مرة."""
    # متاح لكل المستخدمين (يعرض روابط الأدلة الأخرى حسب صلاحيات المستخدم)
    return render_template('help/system_master_guide.html')

@users_bp.route('/help/leaves', methods=['GET'])
@login_required
def help_leaves_guide():
    """دليل الإجازات (داخل النظام) للموظفين ولمسؤولي الموارد البشرية."""
    return render_template('help/leaves_guide.html')


@users_bp.route('/help/employee', methods=['GET'])
@login_required
def help_employee_guide():
    """دليل الموظف (داخل النظام)."""
    return render_template('help/employee_guide.html')


@users_bp.route('/help/hr-admin', methods=['GET'])
@login_required
def help_hr_admin_guide():
    """دليل أدمن الموارد البشرية (داخل النظام)."""
    # Must be visible only to HR admins
    _require_any_perm(
        'HR_MASTERDATA_MANAGE',
        'HR_EMPLOYEE_MANAGE',
        'HR_EMPLOYEE_ATTACHMENTS_MANAGE',
        'HR_DOCS_MANAGE',
        'HR_REQUESTS_VIEW_ALL',
        'HR_PERFORMANCE_MANAGE',
    )
    return render_template('help/hr_admin_guide.html')


@users_bp.route('/help/org-structure-dynamic', methods=['GET'])
@login_required
def help_org_structure_dynamic_guide():
    """دليل الهيكلية الموحّدة (ديناميكية)."""
    # HR/Admin/Super roles can view this guide even if role-permissions were not synced yet.
    try:
        if current_user.has_role('HR_ADMIN') or current_user.has_role('ADMIN') or current_user.has_role('SUPER_ADMIN') or current_user.has_role('SUPERADMIN'):
            return render_template('help/org_dynamic_guide.html')
    except Exception:
        pass
    # Some deployments store roles in Arabic display names (e.g., "أدمن الموارد البشرية").
    try:
        role_raw = (getattr(current_user, 'role', '') or '')
        if ('أدمن' in role_raw and ('الموارد البشرية' in role_raw or 'الموارد' in role_raw)):
            return render_template('help/org_dynamic_guide.html')
    except Exception:
        pass

    _require_any_perm(
        'HR_ORG_DYNAMIC_GUIDE_VIEW',
        'HR_ORGSTRUCTURE_MANAGE',
        'HR_MASTERDATA_MANAGE',
        'PORTAL_ADMIN_PERMISSIONS_MANAGE',
    )
    return render_template('help/org_dynamic_guide.html')


@users_bp.route('/help/performance', methods=['GET'])
@login_required
def help_performance_guide():
    """دليل الأداء والتقييم (للموظف): تقييم النموذج + التقييم النظامي."""
    _require_any_perm(
        'HR_PERFORMANCE_READ',
        'HR_PERFORMANCE_SUBMIT',
        'HR_PERFORMANCE_MANAGE',
        'HR_SYSTEM_EVALUATION_VIEW',
    )
    return render_template('help/performance_guide.html')


@users_bp.route('/help/performance-admin', methods=['GET'])
@login_required
def help_performance_admin_guide():
    """دليل الأداء والتقييم (للأدمن): إدارة النماذج والدورات + تشغيل التقييم النظامي."""
    _require_any_perm(
        'HR_PERFORMANCE_MANAGE',
        'PORTAL_ADMIN_PERMISSIONS_MANAGE',
    )
    return render_template('help/performance_admin_guide.html')


@users_bp.route('/help/transport', methods=['GET'])
@login_required
def help_transport_guide():
    """دليل الحركة والنقل (داخل النظام)."""
    return render_template('help/transport_guide.html')


@users_bp.route('/help/transport-admin', methods=['GET'])
@login_required
def help_transport_admin_guide():
    """دليل أدمن/مشرف الحركة والنقل (داخل النظام)."""
    _require_any_perm('TRANSPORT_UPDATE', 'TRANSPORT_DELETE', 'TRANSPORT_TRACKING_MANAGE', 'TRANSPORT_APPROVE')
    return render_template('help/transport_admin_guide.html')



@users_bp.route('/help', methods=['GET'])
@login_required
def help_index():
    '''مركز الأدلة داخل النظام (يعرض الأدلة حسب الصلاحيات).'''
    return render_template('help/index.html')


def _has_any_perm(*keys: str) -> bool:
    for k in keys:
        if not k:
            continue
        try:
            if current_user.has_perm(k):
                return True
        except Exception:
            continue
    return False


def _require_any_perm(*keys: str):
    # SUPER ADMIN bypass (supports legacy naming)
    try:
        if current_user.has_role('SUPER_ADMIN') or current_user.has_role('SUPERADMIN'):
            return
    except Exception:
        pass
    if not _has_any_perm(*keys):
        abort(403)


@users_bp.route('/help/workflow', methods=['GET'])
@login_required
def help_workflow_guide():
    '''دليل المسارات + أنواع الطلبات + قواعد التوجيه.'''
    _require_any_perm('WORKFLOW_TEMPLATES_READ', 'WORKFLOW_TEMPLATES_MANAGE',
                      'WORKFLOW_ROUTING_READ', 'WORKFLOW_ROUTING_MANAGE',
                      'REQUEST_TYPES_READ')
    return render_template('help/workflow_guide.html')


@users_bp.route('/help/workflow-user', methods=['GET'])
@login_required
def help_workflow_user_guide():
    """دليل المستخدم (الطلبات والمسارات) داخل مسار."""
    # متاح لكل المستخدمين (لا يكشف إعدادات حساسة)
    return render_template('help/workflow_user_guide.html')


@users_bp.route('/help/correspondence', methods=['GET'])
@login_required
def help_correspondence_guide():
    '''دليل الصادر والوارد.'''
    _require_any_perm('CORR_READ')
    return render_template('help/corr_guide.html')


@users_bp.route('/help/correspondence-admin', methods=['GET'])
@login_required
def help_correspondence_admin_guide():
    '''دليل أدمن الصادر والوارد (إعدادات/تصنيفات/صلاحيات).'''
    _require_any_perm('CORR_LOOKUPS_MANAGE', 'CORR_UPDATE', 'CORR_DELETE', 'CORR_EXPORT')
    return render_template('help/corr_admin_guide.html')


@users_bp.route('/help/hr', methods=['GET'])
@login_required
def help_hr_guide():
    '''دليل الموارد البشرية (حسب صلاحيات المستخدم).'''
    _require_any_perm('HR_READ', 'HR_REQUESTS_READ', 'HR_REQUESTS_VIEW_ALL',
                      'HR_EMPLOYEE_READ', 'HR_EMP_MANAGE', 'HR_MASTERDATA_MANAGE')
    return render_template('help/hr_guide.html')


@users_bp.route('/help/store', methods=['GET'])
@login_required
def help_store_guide():
    '''دليل المستودع.'''
    _require_any_perm('STORE_READ', 'STORE_MANAGE')
    return render_template('help/store_guide.html')


@users_bp.route('/help/store-admin', methods=['GET'])
@login_required
def help_store_admin_guide():
    '''دليل أدمن المستودع (سياسات/إدارة/تصدير).'''
    _require_any_perm('STORE_MANAGE', 'STORE_EXPORT')
    return render_template('help/store_admin_guide.html')


@users_bp.route('/help/access-requests', methods=['GET'])
@login_required
def help_access_requests_guide():
    '''دليل طلبات الصلاحيات.'''
    _require_any_perm('PORTAL_ADMIN_PERMISSIONS_MANAGE', 'USER_PERMISSIONS_MANAGE', 'PORTAL_ADMIN_READ')
    return render_template('help/access_requests_guide.html')


@users_bp.route('/help/access-requests-admin', methods=['GET'])
@login_required
def help_access_requests_admin_guide():
    '''دليل أدمن طلبات الصلاحيات (الأدوار/الصلاحيات/المراجعة).'''
    _require_any_perm('PORTAL_ADMIN_PERMISSIONS_MANAGE', 'USER_PERMISSIONS_MANAGE', 'PORTAL_ADMIN_READ')
    return render_template('help/access_requests_admin_guide.html')


@users_bp.route('/help/workflow-admin', methods=['GET'])
@login_required
def help_workflow_admin_guide():
    '''Alias to admin workflow guide (templates/routing).'''
    _require_any_perm('WORKFLOW_TEMPLATES_READ', 'WORKFLOW_TEMPLATES_MANAGE',
                      'WORKFLOW_ROUTING_READ', 'WORKFLOW_ROUTING_MANAGE',
                      'REQUEST_TYPES_READ')
    return render_template('help/workflow_guide.html')





def _get_role_choices(include_adminish: bool) -> list[str]:
    """Read roles from DB (Role table)."""
    try:
        roles = Role.query.filter_by(is_active=True).order_by(Role.id.asc()).all()
        codes = [r.code for r in roles if r.code]
    except Exception:
        codes = []

    # Ensure core roles exist in UI even if not seeded yet
    for c in ["USER", "dept_head", "directorate_head"]:
        if c not in codes:
            codes.append(c)

    if include_adminish:
        for c in ["ADMIN", "SUPER_ADMIN"]:
            if c not in codes:
                codes.append(c)
    else:
        codes = [c for c in codes if c not in ("ADMIN", "SUPER_ADMIN")]

    # stable ordering (case-insensitive)
    codes_sorted = sorted(codes, key=lambda x: (str(x).lower()))
    return codes_sorted




def _allowed_avatar(filename: str) -> bool:
    if not filename or "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower().strip()
    return ext in AVATAR_ALLOWED_EXTS


def _avatar_storage_dir() -> str:
    base = os.path.join(current_app.root_path, "static", "uploads", "avatars")
    os.makedirs(base, exist_ok=True)
    return base


def _employee_upload_dir(user_id: int) -> str:
    # Same storage used by Portal HR (instance/uploads/employees/<user_id>)
    base = Path(current_app.instance_path) / "uploads" / "employees" / str(int(user_id))
    base.mkdir(parents=True, exist_ok=True)
    return str(base)




def _is_super_admin():
    return (getattr(current_user, "role", "") or "").strip().upper() == "SUPER_ADMIN"


def _resolve_user_org(u: User):
    # Returns: (organization, directorate, unit, department, section, division)
    dept = None
    directorate = None
    unit = None
    section = None
    division = None
    organization = None

    try:
        if getattr(u, 'department_id', None):
            dept = Department.query.get(int(u.department_id))
    except Exception:
        dept = None

    try:
        if getattr(u, 'directorate_id', None):
            directorate = Directorate.query.get(int(u.directorate_id))
        elif dept and getattr(dept, 'directorate_id', None):
            directorate = Directorate.query.get(int(dept.directorate_id))
    except Exception:
        directorate = None

    try:
        if getattr(u, 'unit_id', None):
            unit = Unit.query.get(int(u.unit_id))
        elif dept and getattr(dept, 'unit_id', None):
            unit = Unit.query.get(int(dept.unit_id))
    except Exception:
        unit = None

    try:
        if getattr(u, 'section_id', None):
            section = Section.query.get(int(u.section_id))
    except Exception:
        section = None

    try:
        if getattr(u, 'division_id', None):
            division = Division.query.get(int(u.division_id))
    except Exception:
        division = None

    try:
        if unit and getattr(unit, 'organization_id', None):
            organization = Organization.query.get(int(unit.organization_id))
        elif directorate and getattr(directorate, 'organization_id', None):
            organization = Organization.query.get(int(directorate.organization_id))
    except Exception:
        organization = None

    return organization, directorate, unit, dept, section, division



def _validate_role(role: str) -> bool:
    if not role:
        return False
    role = str(role).strip()
    if not role:
        return False

    # Admin-ish are always allowed as values (enforced by routes)
    if role in ("ADMIN", "SUPER_ADMIN"):
        return True

    r = Role.query.filter_by(code=role).first()
    return bool(r and r.is_active)
def _audit(action: str, target_user: User, note: str):
    db.session.add(AuditLog(
        action=action,
        user_id=current_user.id,
        target_type="User",
        target_id=target_user.id,
        note=note
    ))


@users_bp.route("/")
@login_required
@roles_required("ADMIN")
def list_users():
    page = request.args.get("page", 1, type=int)
    q = (request.args.get("q") or "").strip()

    query = User.query

    # Simple search (id/email/name/job_title)
    if q:
        like = f"%{q}%"
        conds = [
            User.email.ilike(like),
            User.name.ilike(like),
            User.job_title.ilike(like),
        ]
        if q.isdigit():
            try:
                conds.insert(0, User.id == int(q))
            except Exception:
                pass
        query = query.filter(or_(*conds))

    pagination = (
        query
        .order_by(User.id.desc())
        .paginate(page=page, per_page=25, error_out=False)
    )

    users_with_role = []
    for u in pagination.items:
        users_with_role.append({
            "id": u.id,
            "email": u.email,
            "name": getattr(u, "name", None),
            "job_title": getattr(u, "job_title", None),
            "role": u.role,
        })

    roles_for_ui = _get_role_choices(include_adminish=_is_super_admin())

    return render_template(
        "users/list.html",
        users=users_with_role,
        pagination=pagination,
        role_choices=roles_for_ui,
        is_super_admin=_is_super_admin(),
        q=q,
    )


@users_bp.route("/export.xlsx")
@login_required
@roles_required("ADMIN")
def export_users_excel():
    """Export Users list to Excel (respects current filters).

    Filters supported:
      - q: search by id/email/name/job_title
    """
    q = (request.args.get("q") or "").strip()

    query = User.query

    if q:
        like = f"%{q}%"
        conds = [
            User.email.ilike(like),
            User.name.ilike(like),
            User.job_title.ilike(like),
        ]
        if q.isdigit():
            try:
                conds.insert(0, User.id == int(q))
            except Exception:
                pass
        query = query.filter(or_(*conds))

    users = query.order_by(User.id.desc()).all()

    # Lookup names for department/directorate
    dept_map = {d.id: (d.name_ar or d.name_en or str(d.id)) for d in Department.query.all()}
    dir_map = {d.id: (d.name_ar or d.name_en or str(d.id)) for d in Directorate.query.all()}

    headers = [
        "ID",
        "Email",
        "Name",
        "Job Title",
        "Role",
        "Department",
        "Directorate",
    ]

    rows = []
    for u in users:
        dept_name = dept_map.get(getattr(u, "department_id", None), "")
        # If directorate_id exists use it; else infer from department->directorate if model has it
        dir_id = getattr(u, "directorate_id", None)
        dir_name = dir_map.get(dir_id, "")
        rows.append([
            u.id,
            u.email,
            getattr(u, "name", "") or "",
            getattr(u, "job_title", "") or "",
            u.role,
            dept_name,
            dir_name,
        ])

    data = make_xlsx_bytes("Users", headers, rows)
    filename = f"users_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.xlsx"
    return send_file(
        BytesIO(data),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )




@users_bp.route("/import-excel", methods=["POST"])
@login_required
@roles_required("ADMIN")
def import_users_excel():
    """Import users from Excel with Replace/Safe Replace.

    Expected columns (any naming variant is accepted):
      - Email (required)
      - Name
      - Job Title
      - Role
      - Department Code / Department ID / Department
      - Directorate Code / Directorate ID / Directorate
      - Password (optional; for new users)

    Modes:
      - Safe Replace: upsert only (no delete)
      - Replace: upsert, then best-effort delete users NOT present in file (skips protected / referenced)
    """
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy import func
    from utils import importer as xl

    mode = (request.form.get("mode") or "safe").strip().lower()
    if mode not in ("replace", "safe"):
        mode = "safe"

    f = request.files.get("file")
    if not f or not getattr(f, "filename", ""):
        flash("اختر ملف Excel (.xlsx)", "danger")
        return redirect(url_for("users.list_users"))

    try:
        _sheet, rows, headers = xl.read_excel_rows(f)
    except Exception as e:
        flash(f"خطأ في قراءة ملف Excel: {e}", "danger")
        return redirect(url_for("users.list_users"))

    # Caches for lookups
    depts = Department.query.all()
    dirs = Directorate.query.all()

    depts_by_id = {d.id: d for d in depts}
    dirs_by_id = {d.id: d for d in dirs}

    def _key(s):
        return (s or "").strip().lower()

    depts_by_code = {_key(getattr(d, "code", None)): d for d in depts if getattr(d, "code", None)}
    dirs_by_code = {_key(getattr(d, "code", None)): d for d in dirs if getattr(d, "code", None)}

    depts_by_name = {_key(getattr(d, "name_ar", None)): d for d in depts if getattr(d, "name_ar", None)}
    depts_by_name.update({_key(getattr(d, "name_en", None)): d for d in depts if getattr(d, "name_en", None)})

    dirs_by_name = {_key(getattr(d, "name_ar", None)): d for d in dirs if getattr(d, "name_ar", None)}
    dirs_by_name.update({_key(getattr(d, "name_en", None)): d for d in dirs if getattr(d, "name_en", None)})

    def _resolve_department(val):
        s = xl.to_str(val)
        if not s:
            return None
        if s.isdigit():
            return depts_by_id.get(int(s))
        return depts_by_code.get(_key(s)) or depts_by_name.get(_key(s))

    def _resolve_directorate(val):
        s = xl.to_str(val)
        if not s:
            return None
        if s.isdigit():
            return dirs_by_id.get(int(s))
        return dirs_by_code.get(_key(s)) or dirs_by_name.get(_key(s))

    protected_ids = {current_user.id}
    # Protect SUPER_ADMIN users from deletion by normal admin
    protected_ids.update([u.id for u in User.query.filter(User.role.in_(["SUPER_ADMIN"]))])

    created = updated = skipped = deleted = 0
    imported_emails = set()

    for r in rows:
        email = xl.to_str(xl.pick(r, "email", "البريد الإلكتروني", "البريد الالكتروني", "mail"))
        if not email:
            skipped += 1
            continue
        email_norm = email.strip().lower()
        imported_emails.add(email_norm)

        name = xl.to_str(xl.pick(r, "name", "الاسم", "full_name"))
        job_title = xl.to_str(xl.pick(r, "job_title", "job title", "المسمى الوظيفي", "الوظيفة"))
        role = xl.to_str(xl.pick(r, "role", "الدور", "role_code"))
        password = xl.to_str(xl.pick(r, "password", "كلمة المرور", "pass"))

        dept_val = xl.pick(r, "department_code", "dept_code", "department", "الدائرة", "department_id")
        dir_val = xl.pick(r, "directorate_code", "dir_code", "directorate", "الإدارة", "الادارة", "directorate_id")

        dep = _resolve_department(dept_val)
        direc = _resolve_directorate(dir_val)

        u = User.query.filter(func.lower(User.email) == email_norm).first()

        if u:
            # Only SUPER_ADMIN can change ADMIN/SUPER_ADMIN accounts role; normal admin can update safe fields
            if (u.role or "").strip().upper() in ("ADMIN", "SUPER_ADMIN") and not _is_super_admin():
                if name is not None:
                    u.name = name
                if job_title is not None:
                    u.job_title = job_title
                if dep:
                    u.department_id = dep.id
                if direc:
                    u.directorate_id = direc.id
                updated += 1
                continue

            if name is not None:
                u.name = name
            if job_title is not None:
                u.job_title = job_title
            if role:
                role = role.strip()
                if role in ("ADMIN", "SUPER_ADMIN") and not _is_super_admin():
                    pass
                else:
                    if _validate_role(role):
                        u.role = role
            if dep:
                u.department_id = dep.id
            if direc:
                u.directorate_id = direc.id
            if password:
                u.set_password(password)
            updated += 1
        else:
            role = (role or "USER").strip()
            if role in ("ADMIN", "SUPER_ADMIN") and not _is_super_admin():
                role = "USER"
            if not _validate_role(role):
                role = "USER"

            temp_password = password or "Temp@12345"
            u = User(email=email.strip(), role=role)
            if name:
                u.name = name
            if job_title:
                u.job_title = job_title
            if dep:
                u.department_id = dep.id
            if direc:
                u.directorate_id = direc.id
            u.set_password(temp_password)
            db.session.add(u)
            created += 1

    db.session.commit()

    if mode == "replace":
        # Best-effort deletion of users not present in Excel.
        # Skips protected IDs, and skips ADMIN/SUPER_ADMIN unless current is SUPER_ADMIN.
        to_delete = (
            User.query
            .filter(~func.lower(User.email).in_(list(imported_emails)))
            .order_by(User.id.asc())
            .all()
        )
        for u in to_delete:
            if u.id in protected_ids:
                continue
            if (u.role or "").strip().upper() in ("ADMIN", "SUPER_ADMIN") and not _is_super_admin():
                continue
            try:
                db.session.delete(u)
                db.session.flush()
                deleted += 1
            except IntegrityError:
                db.session.rollback()
                continue
            except Exception:
                db.session.rollback()
                continue
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

    msg = f"✅ تم الاستيراد. (جديد: {created} / تحديث: {updated}"
    if mode == "replace":
        msg += f" / حذف: {deleted}"
    if skipped:
        msg += f" / تخطي: {skipped}"
    msg += ")"
    flash(msg, "success")
    return redirect(url_for("users.list_users"))

@users_bp.route("/create", methods=["GET", "POST"])
@login_required
@roles_required("ADMIN")
def create_user():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        job_title = request.form.get("job_title", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "").strip()

        if not email or not password or not role:
            flash("يرجى تعبئة جميع الحقول", "danger")
            return redirect(url_for("users.create_user"))

        # Only SUPER_ADMIN can create ADMIN / SUPER_ADMIN users
        if role in ("ADMIN", "SUPER_ADMIN") and not _is_super_admin():
            abort(403)

        if not _validate_role(role):
            flash("الدور المختار غير صالح", "danger")
            return redirect(url_for("users.create_user"))

        if User.query.filter_by(email=email).first():
            flash("المستخدم موجود مسبقًا", "danger")
            return redirect(url_for("users.create_user"))

        user = User(
            name=name or None,
            job_title=job_title or None,
            email=email,
            password_hash=generate_password_hash(password),
            role=role
        )

        db.session.add(user)
        db.session.flush()  # للحصول على user.id

        _audit(
            "USER_CREATED",
            user,
            note=f"User {email} created with role {role}"
        )

        emit_event(
            actor_id=current_user.id,
            action="USER_CREATED",
            message=f"تم إنشاء مستخدم جديد: {user.email}",
            target_type="User",
            target_id=user.id,
            notify_role="ADMIN",
            level="INFO",
            auto_commit=False
        )

        db.session.commit()

        flash("تم إنشاء المستخدم بنجاح", "success")
        return redirect(url_for("users.list_users"))

    # Role options in UI
    roles_for_ui = _get_role_choices(include_adminish=_is_super_admin())
    return render_template("users/create.html", role_choices=roles_for_ui)


@users_bp.route("/<int:user_id>/role", methods=["POST"])
@login_required
@roles_required("ADMIN")
def change_role(user_id):
    user = User.query.get_or_404(user_id)

    old_role = user.role
    new_role = request.form.get("role", "").strip()

    if not new_role:
        flash("الدور الجديد غير موجود", "danger")
        return redirect(url_for("users.list_users"))

    if not _validate_role(new_role):
        flash("الدور المختار غير صالح", "danger")
        return redirect(url_for("users.list_users"))

    # Only SUPER_ADMIN can modify ADMIN/SUPER_ADMIN accounts or assign ADMIN/SUPER_ADMIN
    target_is_adminish = (old_role or "").strip().upper() in ("ADMIN", "SUPER_ADMIN")
    if (target_is_adminish or new_role in ("ADMIN", "SUPER_ADMIN")) and not _is_super_admin():
        abort(403)

    if new_role == old_role:
        flash("لم يتم تغيير الدور (نفس الدور الحالي).", "info")
        return redirect(url_for("users.list_users"))

    user.role = new_role

    _audit(
        "USER_ROLE_CHANGED",
        user,
        note=f"Role changed from {old_role} to {new_role}"
    )

    emit_event(
        actor_id=current_user.id,
        action="USER_ROLE_CHANGED",
        message=f"تم تغيير دور المستخدم {user.email}: {old_role} → {new_role}",
        target_type="User",
        target_id=user.id,
        notify_user_id=user.id,
        level="WARNING",
        auto_commit=False
    )

    db.session.commit()

    flash("تم تحديث الدور", "success")
    return redirect(url_for("users.list_users"))


# =========================
# SUPER ADMIN — Manage Users (admins included)
# =========================
@users_bp.route("/<int:user_id>/manage")
@login_required
@roles_required("SUPER_ADMIN")
def manage_user(user_id):
    user = User.query.get_or_404(user_id)
    roles_for_ui = _get_role_choices(include_adminish=_is_super_admin())
    return render_template("users/manage.html", user=user, role_choices=roles_for_ui)


@users_bp.route("/<int:user_id>/manage/role", methods=["POST"])
@login_required
@roles_required("SUPER_ADMIN")
def manage_user_role(user_id):
    user = User.query.get_or_404(user_id)

    new_role = request.form.get("role", "").strip()
    if not _validate_role(new_role):
        flash("الدور المختار غير صالح", "danger")
        return redirect(url_for("users.manage_user", user_id=user.id))

    old_role = user.role
    if new_role == old_role:
        flash("نفس الدور الحالي.", "info")
        return redirect(url_for("users.manage_user", user_id=user.id))

    # Prevent locking yourself out
    if user.id == current_user.id and new_role != "SUPER_ADMIN":
        flash("لا يمكنك إزالة صلاحية SUPER_ADMIN عن حسابك الحالي.", "danger")
        return redirect(url_for("users.manage_user", user_id=user.id))

    user.role = new_role
    _audit("USER_ROLE_CHANGED", user, note=f"Role changed from {old_role} to {new_role}")

    emit_event(
        actor_id=current_user.id,
        action="USER_ROLE_CHANGED",
        message=f"تم تغيير دور المستخدم {user.email}: {old_role} → {new_role}",
        target_type="User",
        target_id=user.id,
        notify_user_id=user.id,
        level="WARNING",
        auto_commit=False
    )

    db.session.commit()
    flash("تم تحديث الدور", "success")
    return redirect(url_for("users.manage_user", user_id=user.id))


@users_bp.route("/<int:user_id>/manage/password", methods=["POST"])
@login_required
@roles_required("SUPER_ADMIN")
def manage_user_password(user_id):
    user = User.query.get_or_404(user_id)

    new_pw = request.form.get("new_password", "")
    confirm_pw = request.form.get("confirm_password", "")

    if not new_pw or not confirm_pw:
        flash("يرجى تعبئة حقول كلمة المرور", "danger")
        return redirect(url_for("users.manage_user", user_id=user.id))

    if len(new_pw) < 6:
        flash("كلمة المرور يجب أن تكون 6 أحرف على الأقل", "warning")
        return redirect(url_for("users.manage_user", user_id=user.id))

    if new_pw != confirm_pw:
        flash("كلمتا المرور غير متطابقتين", "danger")
        return redirect(url_for("users.manage_user", user_id=user.id))

    user.password_hash = generate_password_hash(new_pw)

    _audit("USER_PASSWORD_RESET", user, note="Password reset by SUPER_ADMIN")

    emit_event(
        actor_id=current_user.id,
        action="USER_PASSWORD_RESET",
        message="تم إعادة تعيين كلمة المرور من قبل Super Admin",
        target_type="User",
        target_id=user.id,
        notify_user_id=user.id,
        level="WARNING",
        auto_commit=False
    )

    db.session.commit()
    flash("تم تحديث كلمة المرور", "success")
    return redirect(url_for("users.manage_user", user_id=user.id))


@users_bp.route("/<int:user_id>/manage/delete", methods=["POST"])
@login_required
@roles_required("SUPER_ADMIN")
def manage_user_delete(user_id):
    user = User.query.get_or_404(user_id)

    if user.id == current_user.id:
        flash("لا يمكنك حذف حسابك الحالي.", "danger")
        return redirect(url_for("users.manage_user", user_id=user.id))

    email = user.email
    role = user.role

    _audit("USER_DELETED", user, note=f"User {email} ({role}) deleted by SUPER_ADMIN")

    emit_event(
        actor_id=current_user.id,
        action="USER_DELETED",
        message=f"تم حذف المستخدم {email}",
        target_type="User",
        target_id=user.id,
        notify_role="ADMIN",
        level="CRITICAL",
        auto_commit=False
    )

    db.session.delete(user)
    db.session.commit()

    flash("تم حذف المستخدم", "success")
    return redirect(url_for("users.list_users"))


# =========================
# My Profile (User self-service)
# =========================
@users_bp.route("/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
@roles_required("ADMIN")
def edit_user(user_id):
    target = User.query.get_or_404(user_id)

    # ✅ Admin cannot touch SUPER_ADMIN (only SUPER_ADMIN can)
    if (getattr(target, "role", "") or "").strip().upper() == "SUPER_ADMIN" and not _is_super_admin():
        abort(403)

    departments = Department.query.filter_by(is_active=True).order_by(Department.name_ar.asc()).all()
    directorates = Directorate.query.filter_by(is_active=True).order_by(Directorate.name_ar.asc()).all()
    units = Unit.query.filter_by(is_active=True).order_by(Unit.name_ar.asc()).all()
    sections = Section.query.filter_by(is_active=True).order_by(Section.name_ar.asc()).all()
    divisions = Division.query.filter_by(is_active=True).order_by(Division.name_ar.asc()).all()

    if request.method == "POST":
        # Basic fields
        target.name = (request.form.get("name") or "").strip() or None
        target.job_title = (request.form.get("job_title") or "").strip() or None
        new_email = (request.form.get("email") or "").strip()
        if new_email:
            target.email = new_email

        # Org assignment (optional) — supports Directorate/Unit/Department/Section/Division
        def _parse_int(field: str):
            raw = request.form.get(field)
            try:
                return int(raw) if raw not in (None, "", "0") else None
            except Exception:
                return None

        division_id = _parse_int("division_id")
        section_id = _parse_int("section_id")
        dept_id = _parse_int("department_id")
        unit_id = _parse_int("unit_id")
        dir_id = _parse_int("directorate_id")

        # Normalize: the most specific selection wins (Division → Section → Department → Unit → Directorate)
        target.division_id = None
        target.section_id = None
        target.department_id = None
        target.unit_id = None
        target.directorate_id = None

        applied = False

        if division_id:
            div = Division.query.get(division_id)
            if div:
                applied = True
                target.division_id = div.id

                # Prefer Division.section_id if present
                if getattr(div, "section_id", None):
                    target.section_id = int(div.section_id)

                # Division may also link directly to a Department
                dep_id = getattr(div, "department_id", None)
                if dep_id:
                    target.department_id = int(dep_id)

                # Derive parents from the best available node
                # 1) From Department (if known)
                if target.department_id:
                    dept = Department.query.get(int(target.department_id))
                    if dept:
                        target.directorate_id = int(dept.directorate_id) if getattr(dept, "directorate_id", None) else None
                        target.unit_id = int(dept.unit_id) if getattr(dept, "unit_id", None) else None

                # 2) Otherwise, from Section
                if (not target.department_id) and target.section_id:
                    sec = Section.query.get(int(target.section_id))
                    if sec:
                        if getattr(sec, "department_id", None):
                            target.department_id = int(sec.department_id)
                            dept = Department.query.get(int(sec.department_id))
                            if dept:
                                target.directorate_id = int(dept.directorate_id) if getattr(dept, "directorate_id", None) else None
                                target.unit_id = int(dept.unit_id) if getattr(dept, "unit_id", None) else None
                        else:
                            target.directorate_id = int(sec.directorate_id) if getattr(sec, "directorate_id", None) else None
                            target.unit_id = int(sec.unit_id) if getattr(sec, "unit_id", None) else None

        if (not applied) and section_id:
            sec = Section.query.get(section_id)
            if sec:
                applied = True
                target.section_id = sec.id
                if getattr(sec, "department_id", None):
                    target.department_id = int(sec.department_id)
                    dept = Department.query.get(int(sec.department_id))
                    if dept:
                        target.directorate_id = int(dept.directorate_id) if getattr(dept, "directorate_id", None) else None
                        target.unit_id = int(dept.unit_id) if getattr(dept, "unit_id", None) else None
                else:
                    target.directorate_id = int(sec.directorate_id) if getattr(sec, "directorate_id", None) else None
                    target.unit_id = int(sec.unit_id) if getattr(sec, "unit_id", None) else None

        if (not applied) and dept_id:
            dept = Department.query.get(dept_id)
            target.department_id = dept_id
            applied = True
            if dept:
                target.directorate_id = int(dept.directorate_id) if getattr(dept, "directorate_id", None) else None
                target.unit_id = int(dept.unit_id) if getattr(dept, "unit_id", None) else None

        if (not applied) and unit_id:
            target.unit_id = unit_id
            applied = True

        if (not applied) and dir_id:
            target.directorate_id = dir_id
            applied = True

# Password reset (optional)
        new_pw = (request.form.get("new_password") or "").strip()
        if new_pw:
            target.password_hash = generate_password_hash(new_pw)
            _audit("RESET_PASSWORD", target, f"Password reset by admin (user_id={current_user.id})")

            # notify user
            try:
                emit_event(
                        actor_id=current_user.id,
                        action="RESET_PASSWORD",
                        message="تمت إعادة تعيين كلمة المرور الخاصة بك بواسطة الإدارة.",
                        target_type="User",
                        target_id=target.id,
                        notify_user_id=target.id,
                        level="INFO",
                        auto_commit=False
                    )
            except Exception:
                pass

        _audit("UPDATE_USER", target, "Admin updated user profile fields")

        try:
            db.session.commit()
            flash("تم تحديث بيانات المستخدم بنجاح.", "success")
        except Exception as e:
            db.session.rollback()
            flash(f"تعذر تحديث المستخدم: {e}", "danger")

        return redirect(url_for("users.list_users"))

    return render_template(
        "users/edit.html",
        u=target,
        departments=departments,
        directorates=directorates,
        units=units,
        sections=sections,
        divisions=divisions,
        is_super_admin=_is_super_admin(),
    )

@users_bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    # User profile page.

    organization, directorate, unit, dept, section, division = _resolve_user_org(current_user)

    # Employee file (managed from Portal HR)
    emp_file = None
    emp_atts = []
    try:
        emp_file = EmployeeFile.query.filter_by(user_id=current_user.id).first()
        emp_atts = EmployeeAttachment.query.filter_by(user_id=current_user.id).order_by(EmployeeAttachment.uploaded_at.desc()).all()
    except Exception:
        emp_file = None
        emp_atts = []

    if request.method == "POST":

        form_type = (request.form.get("form_type") or "").strip().lower()

        # ------- Update profile info (name/job/email)
        if form_type in ("profile", ""):
            new_name = (request.form.get("name") or "").strip()
            new_job_title = (request.form.get("job_title") or "").strip()
            new_email = (request.form.get("email") or "").strip()
            current_pw = request.form.get("current_password", "")

            if not new_email or not current_pw:
                flash("يرجى تعبئة البريد وكلمة المرور الحالية", "danger")
                return redirect(url_for("users.profile"))

            if not current_user.check_password(current_pw):
                flash("كلمة المرور الحالية غير صحيحة", "danger")
                return redirect(url_for("users.profile"))

            u = User.query.get(current_user.id)

            # email change (ensure unique)
            if new_email != u.email:
                if User.query.filter(User.email == new_email, User.id != u.id).first():
                    flash("هذا البريد مستخدم مسبقًا", "danger")
                    return redirect(url_for("users.profile"))

                old_email = u.email
                u.email = new_email
                if new_name:
                    u.name = new_name
                if new_job_title:
                    u.job_title = new_job_title

                db.session.add(AuditLog(
                    action="USER_PROFILE_UPDATED",
                    user_id=current_user.id,
                    target_type="User",
                    target_id=current_user.id,
                    note=f"Email changed: {old_email} → {new_email}"
                ))

                emit_event(
                    actor_id=current_user.id,
                    action="USER_PROFILE_UPDATED",
                    message="تم تحديث بيانات الملف الشخصي",
                    target_type="User",
                    target_id=current_user.id,
                    notify_user_id=current_user.id,
                    level="INFO",
                    auto_commit=False
                )

                db.session.commit()
                flash("تم تحديث البريد الإلكتروني", "success")
                return redirect(url_for("users.profile"))

            # name/job only
            changed = False
            if new_name and (new_name != (u.name or "").strip()):
                u.name = new_name
                changed = True
            if new_job_title and (new_job_title != (u.job_title or "").strip()):
                u.job_title = new_job_title
                changed = True

            if changed:
                db.session.add(AuditLog(
                    action="USER_PROFILE_UPDATED",
                    user_id=current_user.id,
                    target_type="User",
                    target_id=current_user.id,
                    note="Profile updated (name/title)"
                ))
                db.session.commit()
                flash("تم تحديث بيانات الملف الشخصي", "success")
            else:
                flash("لا يوجد تغيير على البيانات", "info")

            return redirect(url_for("users.profile"))

        # ------- Update avatar
        if form_type == "avatar":
            f = request.files.get("avatar")
            if not f or not getattr(f, "filename", ""):
                flash("يرجى اختيار صورة.", "danger")
                return redirect(url_for("users.profile"))

            if not _allowed_avatar(f.filename):
                flash("امتداد الصورة غير مسموح. المسموح: png, jpg, jpeg, gif, webp", "danger")
                return redirect(url_for("users.profile"))

            ext = f.filename.rsplit(".", 1)[1].lower().strip()
            new_name = secure_filename(f"user_{current_user.id}_{int(time.time())}.{ext}")
            folder = _avatar_storage_dir()

            # delete old avatar
            old = getattr(current_user, "avatar_filename", None)
            if old:
                try:
                    old_path = os.path.join(folder, old)
                    if os.path.exists(old_path):
                        os.remove(old_path)
                except Exception:
                    pass

            f.save(os.path.join(folder, new_name))
            current_user.avatar_filename = new_name

            db.session.add(AuditLog(
                action="USER_AVATAR_UPDATED",
                user_id=current_user.id,
                target_type="User",
                target_id=current_user.id,
                note="Avatar updated"
            ))

            emit_event(
                actor_id=current_user.id,
                action="USER_AVATAR_UPDATED",
                message="تم تحديث صورة الملف الشخصي",
                target_type="User",
                target_id=current_user.id,
                notify_user_id=current_user.id,
                level="INFO",
                auto_commit=False
            )

            db.session.commit()
            flash("تم تحديث صورة الملف الشخصي.", "success")
            return redirect(url_for("users.profile"))

        # ------- Change password
        if form_type == "password":
            current_pw = request.form.get("current_password", "")
            new_pw = request.form.get("new_password", "")
            confirm_pw = request.form.get("confirm_password", "")

            if not current_pw or not new_pw or not confirm_pw:
                flash("يرجى تعبئة جميع حقول كلمة المرور", "danger")
                return redirect(url_for("users.profile") + "#password")

            if not current_user.check_password(current_pw):
                flash("كلمة المرور الحالية غير صحيحة", "danger")
                return redirect(url_for("users.profile") + "#password")

            if len(new_pw) < 6:
                flash("كلمة المرور الجديدة يجب أن تكون 6 أحرف على الأقل", "warning")
                return redirect(url_for("users.profile") + "#password")

            if new_pw != confirm_pw:
                flash("كلمتا المرور غير متطابقتين", "danger")
                return redirect(url_for("users.profile") + "#password")

            u = User.query.get(current_user.id)
            u.password_hash = generate_password_hash(new_pw)

            db.session.add(AuditLog(
                action="USER_PASSWORD_CHANGED",
                user_id=current_user.id,
                target_type="User",
                target_id=current_user.id,
                note="Password changed by user"
            ))

            emit_event(
                actor_id=current_user.id,
                action="USER_PASSWORD_CHANGED",
                message="تم تغيير كلمة المرور",
                target_type="User",
                target_id=current_user.id,
                notify_user_id=current_user.id,
                level="WARNING",
                auto_commit=False
            )

            db.session.commit()
            flash("تم تغيير كلمة المرور.", "success")
            return redirect(url_for("users.profile") + "#password")

        flash("طلب غير معروف.", "danger")
        return redirect(url_for("users.profile"))

    return render_template("users/profile.html", dept=dept, directorate=directorate, organization=organization, unit=unit, section=section, division=division, emp_file=emp_file, emp_atts=emp_atts)


@users_bp.route("/<int:user_id>/profile-view")
@login_required
def profile_view(user_id: int):
    # Read-only employee profile view (for HR/Admin) + self.
    u = User.query.get_or_404(user_id)

    allowed = (u.id == current_user.id)
    if not allowed:
        try:
            if current_user.has_perm('HR_EMP_READ') or current_user.has_perm('HR_EMP_MANAGE'):
                allowed = True
        except Exception:
            pass
        try:
            if (getattr(current_user, 'role', '') or '').strip().upper() in ('ADMIN','SUPER_ADMIN','SUPERADMIN'):
                allowed = True
        except Exception:
            pass

    if not allowed:
        abort(403)

    organization, directorate, unit, dept, section, division = _resolve_user_org(u)

    emp_file = None
    emp_atts = []
    try:
        emp_file = EmployeeFile.query.filter_by(user_id=u.id).first()
        emp_atts = EmployeeAttachment.query.filter_by(user_id=u.id).order_by(EmployeeAttachment.uploaded_at.desc()).all()
    except Exception:
        emp_file = None
        emp_atts = []

    return render_template(
        "users/profile_view.html",
        u=u,
        organization=organization,
        directorate=directorate,
        unit=unit,
        dept=dept,
        section=section,
        division=division,
        emp_file=emp_file,
        emp_atts=emp_atts,
    )


@users_bp.route("/<int:user_id>/attachments/<int:att_id>/download")
@login_required
def user_attachment_download(user_id: int, att_id: int):
    # Download EmployeeAttachment for self or HR/Admin.
    u = User.query.get_or_404(user_id)

    allowed = (u.id == current_user.id)
    if not allowed:
        try:
            if current_user.has_perm('HR_EMP_READ') or current_user.has_perm('HR_EMP_MANAGE'):
                allowed = True
        except Exception:
            pass
        try:
            if (getattr(current_user, 'role', '') or '').strip().upper() in ('ADMIN','SUPER_ADMIN','SUPERADMIN'):
                allowed = True
        except Exception:
            pass

    if not allowed:
        abort(403)

    att = EmployeeAttachment.query.filter_by(id=att_id, user_id=user_id).first_or_404()
    folder = _employee_upload_dir(user_id)
    return send_from_directory(folder, att.stored_name, as_attachment=True, download_name=att.original_name)

@users_bp.route("/change-password")
@login_required
def change_password_redirect():
    """Back-compat / direct link: redirect to profile."""
    return redirect(url_for("users.profile") + "#password")
