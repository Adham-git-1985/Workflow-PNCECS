from werkzeug.security import generate_password_hash
from utils.permissions import admin_required
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required
from models import User
from extensions import db




@users_bp.route("/users")
@login_required
@admin_required
def list_users():
    users = User.query.order_by(User.id.desc()).all()
    return render_template("users/list.html", users=users)


@users_bp.route("/users/create", methods=["GET", "POST"])
@login_required
@admin_required
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
        db.session.commit()

        flash("تم إنشاء المستخدم بنجاح", "success")
        return redirect(url_for("users.list_users"))

    return render_template("users/create.html")


@users_bp.route("/users/<int:user_id>/role", methods=["POST"])
@login_required
@admin_required
def change_role(user_id):
    user = User.query.get_or_404(user_id)

    new_role = request.form["role"]
    user.role = new_role

    db.session.commit()

    flash("تم تحديث الدور", "success")
    return redirect(url_for("users.list_users"))

