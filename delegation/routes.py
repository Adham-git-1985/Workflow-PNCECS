from datetime import datetime, timedelta

from flask import render_template, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user
from sqlalchemy import func

from . import delegation_bp
from extensions import db
from models import Delegation, User, AuditLog, RolePermission



def _is_admin_user() -> bool:
    try:
        return current_user.has_role("ADMIN") or current_user.has_role("SUPER_ADMIN")
    except Exception:
        return False


def _has_permission(key: str) -> bool:
    """Check permission for current user using:
    - ADMIN/SUPER_ADMIN role bypass
    - per-user permissions (UserPermission via current_user.has_perm)
    - per-role permissions (RolePermission table)
    """
    try:
        if current_user.has_role("ADMIN") or current_user.has_role("SUPER_ADMIN"):
            return True
    except Exception:
        pass

    key = (key or "").strip().upper()
    if not key:
        return False

    # Per-user permission
    try:
        if current_user.has_perm(key):
            return True
    except Exception:
        pass

    # Per-role permission
    try:
        role = (getattr(current_user, "role", "") or "").strip().lower()
        if not role:
            return False

        ok = (
            RolePermission.query
            .filter(func.lower(RolePermission.role) == role)
            .filter(RolePermission.permission == key)
            .first()
        )
        return bool(ok)
    except Exception:
        return False


def _can_manage_delegations() -> bool:
    return _has_permission("DELEGATION_MANAGE")


def _can_self_delegate() -> bool:
    return _has_permission("DELEGATION_SELF")


def _can_access_delegations_page() -> bool:
    return _can_manage_delegations() or _can_self_delegate()


def _parse_date(s: str):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


def _parse_dt_local(s: str):
    # HTML datetime-local -> "YYYY-MM-DDTHH:MM"
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


@delegation_bp.route("/", methods=["GET"])
@login_required
def index():
    can_manage = _can_manage_delegations()
    can_self = _can_self_delegate()
    # إذا كان المستخدم يملك DELEGATION_SELF (وليس Admin/SuperAdmin) نُجبر وضع التفويض الذاتي
    if can_self and not _is_admin_user():
        can_manage = False
    # إذا كان المستخدم يملك DELEGATION_SELF (وليس Admin/SuperAdmin) نُجبر وضع التفويض الذاتي
    if can_self and not _is_admin_user():
        can_manage = False
    # إذا كان المستخدم يملك DELEGATION_SELF (وليس Admin/SuperAdmin) نُجبر وضع التفويض الذاتي
    if can_self and not _is_admin_user():
        can_manage = False
    if not (can_manage or can_self):
        abort(403)

    now = datetime.now()

    if can_manage:
        delegations = (
            Delegation.query
            .order_by(Delegation.id.desc())
            .all()
        )
        users = User.query.order_by(User.id.asc()).all()
    else:
        # Self-delegation mode: show only delegations created by this user (as delegator)
        delegations = (
            Delegation.query
            .filter(Delegation.from_user_id == current_user.id)
            .order_by(Delegation.id.desc())
            .all()
        )
        users = User.query.order_by(User.id.asc()).all()

    return render_template(
        "delegation/index.html",
        delegations=delegations,
        users=users,
        now=now,
        can_manage=can_manage,
        can_self=can_self,
    )


@delegation_bp.route("/create", methods=["POST"])
@login_required
def create():
    can_manage = _can_manage_delegations()
    can_self = _can_self_delegate()
    if not (can_manage or can_self):
        abort(403)

    from_user_id = request.form.get("from_user_id", type=int)
    to_user_id = request.form.get("to_user_id", type=int)

    if not can_manage:
        # Self-delegation: delegator must be the current user
        from_user_id = current_user.id
    mode = (request.form.get("mode") or "days").strip().lower()
    note = (request.form.get("note") or "").strip()

    if can_manage and not from_user_id:
        flash("يرجى اختيار المستخدم الأصيل.", "danger")
        return redirect(url_for("delegation.index"))

    if not to_user_id:
        flash("يرجى اختيار المستخدم المفوّض إليه.", "danger")
        return redirect(url_for("delegation.index"))

    if from_user_id == to_user_id:
        flash("لا يمكن عمل تفويض للنفس.", "danger")
        return redirect(url_for("delegation.index"))

    starts_at = None
    expires_at = None

    if mode == "day":
        day = _parse_date(request.form.get("day") or "")
        if not day:
            flash("يرجى اختيار يوم صحيح.", "danger")
            return redirect(url_for("delegation.index"))

        starts_at = datetime(day.year, day.month, day.day, 0, 0, 0)
        expires_at = datetime(day.year, day.month, day.day, 23, 59, 59)

    elif mode == "days":
        start_day = _parse_date(request.form.get("start_day") or "")
        days_count = request.form.get("days_count", type=int) or 0
        if not start_day or days_count <= 0:
            flash("يرجى إدخال تاريخ بداية وعدد أيام صحيح.", "danger")
            return redirect(url_for("delegation.index"))

        end_day = start_day + timedelta(days=days_count - 1)
        starts_at = datetime(start_day.year, start_day.month, start_day.day, 0, 0, 0)
        expires_at = datetime(end_day.year, end_day.month, end_day.day, 23, 59, 59)

    else:  # mode == "expiry"
        sdt = _parse_dt_local(request.form.get("start_dt") or "")
        edt = _parse_dt_local(request.form.get("end_dt") or "")
        if not sdt or not edt:
            flash("يرجى إدخال وقت بداية ونهاية صحيح.", "danger")
            return redirect(url_for("delegation.index"))

        starts_at, expires_at = sdt, edt

    if expires_at < starts_at:
        flash("تاريخ/وقت الانتهاء يجب أن يكون بعد البداية.", "danger")
        return redirect(url_for("delegation.index"))

    # Prevent overlapping active delegations for the same delegatee
    overlap = (
        Delegation.query
        .filter(
            Delegation.to_user_id == to_user_id,
            Delegation.is_active.is_(True),
            Delegation.expires_at >= starts_at,
            Delegation.starts_at <= expires_at,
        )
        .first()
    )
    if overlap:
        flash("يوجد تفويض آخر نشط/متداخل لهذا المستخدم المفوّض إليه.", "warning")
        return redirect(url_for("delegation.index"))

    d = Delegation(
        from_user_id=from_user_id,
        to_user_id=to_user_id,
        starts_at=starts_at,
        expires_at=expires_at,
        is_active=True,
        created_by_id=current_user.id,
        note=note or None,
    )

    try:
        db.session.add(d)
        db.session.flush()

        db.session.add(AuditLog(
            request_id=None,
            user_id=current_user.id,
            action="DELEGATION_CREATE",
            old_status=None,
            new_status=None,
            note=f"تفويض من User#{from_user_id} إلى User#{to_user_id} حتى {expires_at}",
            target_type="DELEGATION",
            target_id=d.id
        ))

        db.session.commit()
        flash("تم إنشاء التفويض بنجاح.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"تعذر إنشاء التفويض: {e}", "danger")

    return redirect(url_for("delegation.index"))


@delegation_bp.route("/<int:delegation_id>/revoke", methods=["POST"])
@login_required
def revoke(delegation_id):
    can_manage = _can_manage_delegations()
    can_self = _can_self_delegate()
    if not (can_manage or can_self):
        abort(403)

    d = Delegation.query.get_or_404(delegation_id)

    if not can_manage and d.from_user_id != current_user.id:
        abort(403)

    try:
        d.is_active = False
        db.session.add(d)

        db.session.add(AuditLog(
            request_id=None,
            user_id=current_user.id,
            action="DELEGATION_REVOKE",
            old_status=None,
            new_status=None,
            note=f"إلغاء التفويض #{d.id}",
            target_type="DELEGATION",
            target_id=d.id
        ))

        db.session.commit()
        flash("تم إلغاء التفويض.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"تعذر إلغاء التفويض: {e}", "danger")

    return redirect(url_for("delegation.index"))
