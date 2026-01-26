from flask import render_template, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user
from werkzeug.security import generate_password_hash

from . import users_bp
from extensions import db
from permissions import roles_required
from models import User, AuditLog, Department, Directorate, Organization
from utils.events import emit_event


ROLE_CHOICES = [
    "USER",
    "dept_head",
    "deputy_head",
    "finance",
    "secretary_general",
    "ADMIN",
    "SUPER_ADMIN",
]


def _is_super_admin():
    return (getattr(current_user, "role", "") or "").strip().upper() == "SUPER_ADMIN"


def _validate_role(role: str) -> bool:
    return role in ROLE_CHOICES


def _audit(action: str, target_user: User, note: str):
    db.session.add(AuditLog(
        action=action,
        user_id=current_user.id,
        target_type="User",
        target_id=target_user.id,
        note=note
    ))


@users_bp.route("/"
)
@login_required
@roles_required("ADMIN")
def list_users():
    page = request.args.get("page", 1, type=int)

    pagination = (
        User.query
        .order_by(User.id.desc())
        .paginate(page=page, per_page=25, error_out=False)
    )

    users_with_role = []
    for u in pagination.items:
        users_with_role.append({
            "id": u.id,
            "email": u.email,
            "role": u.role,
        })

    roles_for_ui = ROLE_CHOICES if _is_super_admin() else [r for r in ROLE_CHOICES if r not in ("ADMIN", "SUPER_ADMIN")]

    return render_template(
        "users/list.html",
        users=users_with_role,
        pagination=pagination,
        role_choices=roles_for_ui,
        is_super_admin=_is_super_admin(),
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
    roles_for_ui = ROLE_CHOICES if _is_super_admin() else [r for r in ROLE_CHOICES if r not in ("ADMIN", "SUPER_ADMIN")]
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
    roles_for_ui = ROLE_CHOICES
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
@users_bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    """User profile page.

    - User can edit email + password.
    - Role is read-only.
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
        form_type = request.form.get("form_type")

        # ------- Update Email (role stays read-only)
        if form_type == "profile":
            new_name = request.form.get("name", "").strip()
            new_job_title = request.form.get("job_title", "").strip()
            new_email = request.form.get("email", "").strip()
            current_pw = request.form.get("current_password", "")

            if not new_email or not current_pw:
                flash("يرجى تعبئة البريد وكلمة المرور الحالية", "danger")
                return redirect(url_for("users.profile"))

            if not current_user.check_password(current_pw):
                flash("كلمة المرور الحالية غير صحيحة", "danger")
                return redirect(url_for("users.profile"))

            if new_email != current_user.email:
                if User.query.filter(User.email == new_email, User.id != current_user.id).first():
                    flash("هذا البريد مستخدم مسبقًا", "danger")
                    return redirect(url_for("users.profile"))

                u = User.query.get(current_user.id)
                old_email = u.email
                old_name = (u.name or "").strip()
                old_job = (u.job_title or "").strip()

                u.email = new_email
                u.name = new_name or u.name
                u.job_title = new_job_title or u.job_title

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
            else:
                u = User.query.get(current_user.id)
                changed = False
                if new_name and (new_name.strip() != (u.name or "").strip()):
                    u.name = new_name.strip()
                    changed = True
                if new_job_title and (new_job_title.strip() != (u.job_title or "").strip()):
                    u.job_title = new_job_title.strip()
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

        # ------- Change Password
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

            user = User.query.get(current_user.id)
            user.password_hash = generate_password_hash(new_pw)

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
                message="تم تغيير كلمة المرور بنجاح",
                target_type="User",
                target_id=current_user.id,
                notify_user_id=current_user.id,
                level="INFO",
                auto_commit=False
            )

            db.session.commit()
            flash("تم تغيير كلمة المرور بنجاح", "success")
            return redirect(url_for("users.profile") + "#password")

        abort(400)

    return render_template(
        "users/profile.html",
        dept=dept,
        directorate=dir_,
        organization=org
    )


@users_bp.route("/change-password")
@login_required
def change_password_redirect():
    """Back-compat / direct link: redirect to profile."""
    return redirect(url_for("users.profile") + "#password")