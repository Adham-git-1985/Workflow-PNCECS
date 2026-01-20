from flask import render_template, request, redirect, url_for, flash
from flask_login import login_required
from werkzeug.security import generate_password_hash

from . import users_bp
from models import User
from extensions import db
from permissions import roles_required
from models import AuditLog
from flask_login import current_user
from utils.events import emit_event


@users_bp.route("/")
@login_required
@roles_required("ADMIN")
def list_users():
    users = User.query.order_by(User.id.desc()).all()
    return render_template("users/list.html", users=users)


@users_bp.route("/create", methods=["GET", "POST"])
@login_required
@roles_required("ADMIN")
def create_user():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]
        role = request.form["role"]

        if User.query.filter_by(email=email).first():
            flash("المستخدم موجود مسبقًا", "danger")
            return redirect(url_for("users.create_user"))

        user = User(
            email=email,
            password_hash=generate_password_hash(password),
            role=role
        )

        db.session.add(user)

        db.session.add(AuditLog(
            action="USER_CREATED",
            user_id=current_user.id,
            target_type="User",
            target_id=user.id,
            description=f"User {email} created with role {role}"
        ))

        # Notification + Audit
        emit_event(
            actor_id=current_user.id,
            action="USER_CREATED",
            message=f"تم إنشاء مستخدم جديد: {user.email}",
            target_type="User",
            target_id=user.id,
            notify_role="ADMIN"
        )

        db.session.commit()

        flash("تم إنشاء المستخدم بنجاح", "success")
        return redirect(url_for("users.list_users"))

    return render_template("users/create.html")


@users_bp.route("/<int:user_id>/role", methods=["POST"])
@login_required
@roles_required("ADMIN")
def change_role(user_id):
    user = User.query.get_or_404(user_id)
    old_role = user.role
    new_role = request.form["role"]

    user.role = new_role

    db.session.add(AuditLog(
        action="USER_ROLE_CHANGED",
        user_id=current_user.id,
        target_type="User",
        target_id=user.id,
        description=f"Role changed from {old_role} to {new_role}"
    ))

    # Notification + Audit
    emit_event(
        actor_id=current_user.id,
        action="USER_ROLE_CHANGED",
        message=f"تم تغيير دور المستخدم {user.email}: {old_role} → {new_role}",
        target_type="User",
        target_id=user.id,
        notify_user_id=user.id,  # المستخدم نفسه
        notif_type="WARNING"
    )

    db.session.commit()

    flash("تم تحديث الدور", "success")
    return redirect(url_for("users.list_users"))
