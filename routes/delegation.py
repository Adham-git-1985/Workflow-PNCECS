from flask import redirect, url_for, flash, render_template, request
from flask_login import login_required, current_user
from models import Delegation, AuditLog, User
from extensions import db
from utils.delegation_rules import validate_delegation
from . import delegation_bp



@delegation_bp.route("/delegation/create", methods=["GET", "POST"])
@login_required
def create_delegation():
    if request.method == "POST":
        to_user_id = request.form.get("to_user_id")
        role = request.form.get("role")
        start_date = request.form.get("start_date")
        end_date = request.form.get("end_date")

        to_user = User.query.get_or_404(to_user_id)

        try:
            validate_delegation(
                from_user_id=current_user.id,
                to_user_id=to_user.id,
                role=role,
                start_date=start_date,
                end_date=end_date
            )
        except ValueError as e:
            flash(str(e), "danger")
            return redirect(url_for("delegation.create_delegation"))

        delegation = Delegation(
            from_user_id=current_user.id,
            to_user_id=to_user.id,
            role=role,
            start_date=start_date,
            end_date=end_date,
            is_active=True
        )

        db.session.add(delegation)

        # 🧾 Audit Log (إنشاء تفويض)
        audit = AuditLog(
            request_id=None,
            user_id=current_user.id,
            action="CREATE_DELEGATION",
            note=f"Delegated role '{role}' to user_id={to_user.id} "
                 f"from {start_date} to {end_date}"
        )

        db.session.add(audit)
        db.session.commit()

        flash("تم إنشاء التفويض بنجاح", "success")
        return redirect(url_for("delegation.list_delegations"))

    users = User.query.filter(User.id != current_user.id).all()
    return render_template("delegation/create.html", users=users)


@delegation_bp.route("/delegations")
@login_required
def list_delegations():
    active = Delegation.query.filter_by(
        from_user_id=current_user.id,
        is_active=True
    ).all()

    expired = Delegation.query.filter_by(
        from_user_id=current_user.id,
        is_active=False
    ).all()

    return render_template(
        "delegation/list.html",
        active=active,
        expired=expired
    )


@delegation_bp.route("/delegation/<int:id>/cancel", methods=["POST"])
@login_required
def cancel_delegation(id):
    delegation = Delegation.query.get_or_404(id)

    # 🔐 تأكد أن المستخدم هو صاحب التفويض
    if delegation.from_user_id != current_user.id:
        flash("غير مصرح لك بإلغاء هذا التفويض", "danger")
        return redirect(url_for("delegation.list_delegations"))

    delegation.is_active = False

    # 🧾 Audit Log
    audit = AuditLog(
        request_id=None,
        user_id=current_user.id,
        action="CANCEL_DELEGATION",
        note=f"Cancelled delegation of role '{delegation.role}' to user_id={delegation.to_user_id}"
    )

    db.session.add(audit)
    db.session.commit()

    flash("تم إلغاء التفويض بنجاح", "success")
    return redirect(url_for("delegation.list_delegations"))

@audit_bp.route("/audit-logs")
@login_required
def list_audit_logs():
    logs = (
        AuditLog.query
        .order_by(AuditLog.created_at.desc())
        .limit(200)
        .all()
    )

    return render_template(
        "audit/list.html",
        logs=logs
    )