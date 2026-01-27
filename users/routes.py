from flask import render_template, request, redirect, url_for, flash, abort, current_app, send_file
from flask_login import login_required, current_user
from werkzeug.security import generate_password_hash
from werkzeug.utils import secure_filename

from . import users_bp
from extensions import db
from permissions import roles_required
from models import User, AuditLog, Department, Directorate, Organization, Role
from utils.events import emit_event

# SQLAlchemy helpers
from sqlalchemy import or_

from io import BytesIO

from utils.excel import make_xlsx_bytes

import os
import time
from datetime import datetime

AVATAR_ALLOWED_EXTS = {"png", "jpg", "jpeg", "gif", "webp"}


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



def _is_super_admin():
    return (getattr(current_user, "role", "") or "").strip().upper() == "SUPER_ADMIN"


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

    if request.method == "POST":
        # Basic fields
        target.name = (request.form.get("name") or "").strip() or None
        target.job_title = (request.form.get("job_title") or "").strip() or None
        new_email = (request.form.get("email") or "").strip()
        if new_email:
            target.email = new_email

        # Department (optional)
        dept_id_raw = request.form.get("department_id")
        try:
            target.department_id = int(dept_id_raw) if dept_id_raw not in (None, "", "0") else None
        except Exception:
            target.department_id = None

        # Directorate (optional) - useful for directorate heads who may not belong to a specific department
        dir_id_raw = request.form.get("directorate_id")
        try:
            target.directorate_id = int(dir_id_raw) if dir_id_raw not in (None, "", "0") else None
        except Exception:
            target.directorate_id = None

        # If directorate_id not explicitly set, try to infer from selected department
        if target.directorate_id is None and target.department_id:
            try:
                dept = Department.query.get(int(target.department_id))
                if dept and getattr(dept, "directorate_id", None) is not None:
                    target.directorate_id = int(dept.directorate_id)
            except Exception:
                pass

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
        is_super_admin=_is_super_admin(),
    )

@users_bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    """User profile page.

    - User can edit: name, job_title, email (with current password confirmation)
    - User can change password
    - User can upload/change avatar
    - Role is read-only
    """

    dept = None
    dir_ = None
    org = None
    if current_user.department_id:
        dept = Department.query.get(current_user.department_id)
        if dept:
            dir_ = Directorate.query.get(dept.directorate_id)
            if dir_:
                org = Organization.query.get(dir_.organization_id)

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

    return render_template("users/profile.html", dept=dept, dir_=dir_, org=org)
@users_bp.route("/change-password")
@login_required
def change_password_redirect():
    """Back-compat / direct link: redirect to profile."""
    return redirect(url_for("users.profile") + "#password")
