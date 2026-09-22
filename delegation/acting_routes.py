"""Web/API routes for scoped acting permissions and formal delegations."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from urllib.parse import urlparse

from flask import flash, g, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from . import delegation_bp
from extensions import db
from models import ArchivedFile, ActingPermission, AuditLog, FormalDelegation, Notification, User
from utils.acting_authorization import (
    ACTIVE,
    ACTING_PERMISSION_CREATE,
    ACTING_PERMISSION_MANAGE,
    ACTION_FIELDS,
    ACTION_LABELS_AR,
    FORMAL_DELEGATION_CREATE,
    FORMAL_DELEGATION_MANAGE,
    FORMAL_REQUIRED_ACTIONS,
    AuthorizationError,
    can_create_acting_permission,
    can_create_formal_delegation,
    clear_execution_context,
    execution_audit_fields,
    expire_due_records,
    get_active_acting_permissions,
    get_active_formal_delegations,
    get_execution_context,
    normalize_action,
    notify_principal_of_execution,
    record_execution_audit,
    select_acting_permission,
    select_formal_delegation,
)
from utils.permissions import (
    clear_legacy_delegation_selection,
    get_available_delegations,
    get_effective_user,
    mark_identity_choice_selected,
    select_legacy_delegation,
)


ACTING_FORM_FIELDS = tuple(
    field for field in ACTION_FIELDS.values() if field != "can_approve"
)
SENSITIVE_ACTION_FIELDS = {"can_reject", "can_cancel", "can_close", "can_reopen"}


def _is_admin(user=None) -> bool:
    user = user or current_user
    try:
        return bool(user.has_role("ADMIN") or user.has_role("SUPER_ADMIN"))
    except Exception:
        role = (getattr(user, "role", "") or "").strip().upper()
        return role in {"ADMIN", "SUPER_ADMIN", "SUPERADMIN"}


def _has_any_permission(user, *keys: str) -> bool:
    if _is_admin(user):
        return True
    try:
        return any(user.has_perm(key) for key in keys)
    except Exception:
        return False


def _can_manage_acting(user=None) -> bool:
    user = user or current_user
    return _has_any_permission(user, ACTING_PERMISSION_CREATE, ACTING_PERMISSION_MANAGE)


def _can_manage_formal(user=None) -> bool:
    user = user or current_user
    return _has_any_permission(user, FORMAL_DELEGATION_CREATE, FORMAL_DELEGATION_MANAGE)


def _parse_datetime(value: str | None) -> datetime | None:
    text = (value or "").strip().replace(" ", "T")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _parse_date(value: str | None) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _date_window(form) -> tuple[datetime | None, datetime | None]:
    starts_at = _parse_datetime(form.get("start_at") or form.get("starts_at"))
    ends_at = _parse_datetime(form.get("end_at") or form.get("ends_at") or form.get("expires_at"))
    if not starts_at:
        starts_at = _parse_datetime(form.get("start_datetime"))
    if not ends_at:
        ends_at = _parse_datetime(form.get("end_datetime"))

    # Accept date-only fields for integrations and simple HTML clients.
    if not starts_at:
        start_day = _parse_date(form.get("start_date"))
        if start_day:
            starts_at = datetime.combine(start_day, datetime.min.time())
    if not ends_at:
        end_day = _parse_date(form.get("end_date"))
        if end_day:
            ends_at = datetime.combine(end_day, datetime.max.time().replace(microsecond=0))
    return starts_at, ends_at


def _int_form(form, *keys) -> int | None:
    for key in keys:
        raw = form.get(key)
        if raw in (None, ""):
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    return None


def _selected_actions(form) -> set[str]:
    selected: set[str] = set()
    for value in form.getlist("actions") + form.getlist("permissions") + form.getlist("action"):
        action = normalize_action(value)
        if action in ACTION_FIELDS and action not in FORMAL_REQUIRED_ACTIONS:
            selected.add(action)

    for action, field in ACTION_FIELDS.items():
        if field == "can_approve":
            continue
        if form.get(field) or form.get(action.lower()):
            selected.add(action)
    return selected


def _permission_values(actions: set[str]) -> dict[str, bool]:
    values = {field: False for field in ACTING_FORM_FIELDS}
    for action in actions:
        field = ACTION_FIELDS.get(action)
        if field in values:
            values[field] = True
    return values


def _next_url(default_endpoint: str) -> str:
    candidate = (request.form.get("next") or request.args.get("next") or "").strip()
    if candidate:
        parsed = urlparse(candidate)
        if not parsed.scheme and not parsed.netloc and candidate.startswith("/"):
            return candidate
    return url_for(default_endpoint)


def _user_label(user: User | None) -> str:
    if not user:
        return "مستخدم"
    return user.full_name or user.username or user.email or f"المستخدم #{user.id}"


def _redelegation_type_code(value: str | None) -> str:
    code = normalize_action(value)
    return {
        "APPROVE": "APPROVAL",
        "موافقة": "CONSENT",
        "اعتماد": "APPROVAL",
        "اعتماد_مالي": "FINANCIAL_APPROVAL",
        "FINANCIAL_APPROVE": "FINANCIAL_APPROVAL",
        "SIGN": "SIGNATURE",
        "DECIDE": "ISSUE_DECISION",
    }.get(code, code)


def _scope_subset(source_value: str | None, requested_value: str | None) -> bool:
    if not source_value:
        return True
    source = {part.strip().upper() for part in str(source_value).replace("|", ",").split(",") if part.strip()}
    requested = {part.strip().upper() for part in str(requested_value or "").replace("|", ",").split(",") if part.strip()}
    return bool(requested) and requested.issubset(source)


def _redelegation_is_within_source(source: FormalDelegation, *, delegation_type: str,
                                    module_id: str | None, scope_type: str | None,
                                    scope_id: str | None, transaction_type: str | None,
                                    request_type: str | None, starts_at: datetime,
                                    ends_at: datetime, allow_redelegation: bool) -> bool:
    if _redelegation_type_code(source.delegation_type) != _redelegation_type_code(delegation_type):
        return False
    if source.module_id and (module_id or "").strip().upper() != source.module_id.strip().upper():
        return False
    if source.scope_id:
        if (scope_type or "ALL").strip().upper() != (source.scope_type or "ALL").strip().upper():
            return False
        if not _scope_subset(source.scope_id, scope_id):
            return False
    if source.transaction_type and not _scope_subset(source.transaction_type, transaction_type):
        return False
    if source.request_type and not _scope_subset(source.request_type, request_type):
        return False
    if source.start_at and starts_at < source.start_at:
        return False
    if source.end_at and ends_at > source.end_at:
        return False
    if allow_redelegation and not bool(source.allow_redelegation):
        return False
    return True


def _grant_notification(
    recipient_id: int,
    message: str,
    *,
    link_url: str | None = None,
    notification_type: str = "DELEGATION",
) -> Notification:
    row = Notification(
        user_id=int(recipient_id),
        message=(message or "").strip()[:255],
        type=notification_type,
        source="workflow",
        link_url=link_url,
        is_read=False,
        is_visible=True,
        email_delivery_mode="GENERAL",
    )
    db.session.add(row)
    return row


@delegation_bp.route("/permissions", methods=["GET"])
@delegation_bp.route("/dashboard", methods=["GET"])
@delegation_bp.route("/acting-permissions", methods=["GET"])
@login_required
def permissions_dashboard():
    """Main dashboard for acting permissions and formal delegations."""
    # Persist automatic expiry so the dashboard and audit/reporting queries do
    # not continue to present elapsed grants as active after the request ends.
    expire_due_records(auto_commit=True)
    now = datetime.utcnow()
    actor_id = int(current_user.id)
    can_manage_acting = _can_manage_acting()
    can_manage_formal = _can_manage_formal()

    active_acting_for_me = get_active_acting_permissions(actor_id, now)
    active_formal_for_me = get_active_formal_delegations(actor_id, now)

    if can_manage_acting:
        granted_by_me = ActingPermission.query.order_by(ActingPermission.id.desc()).limit(500).all()
    else:
        granted_by_me = (
            ActingPermission.query
            .filter(ActingPermission.principal_user_id == actor_id)
            .order_by(ActingPermission.id.desc())
            .limit(500)
            .all()
        )

    if can_manage_formal:
        formal_by_me = FormalDelegation.query.order_by(FormalDelegation.id.desc()).limit(500).all()
    else:
        formal_by_me = (
            FormalDelegation.query
            .filter(FormalDelegation.delegator_user_id == actor_id)
            .order_by(FormalDelegation.id.desc())
            .limit(500)
            .all()
        )

    users = User.query.order_by(User.name.asc().nullslast(), User.email.asc()).all()
    if not can_manage_acting:
        users = [user for user in users if int(user.id) != actor_id]

    operations_for_me = (
        AuditLog.query
        .filter(AuditLog.acting_for_user_id == actor_id)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(50)
        .all()
    )
    delegated_operations_by_me = (
        AuditLog.query
        .filter(AuditLog.formal_delegation_id.in_([
            row.id for row in formal_by_me if row.id
        ]))
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(50)
        .all()
        if any(row.id for row in formal_by_me)
        else []
    )
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    acting_operations_today = AuditLog.query.filter(
        AuditLog.acting_for_user_id == actor_id,
        AuditLog.created_at >= day_start,
    ).count()
    formal_operations_today = AuditLog.query.filter(
        AuditLog.formal_delegation_id.in_([row.id for row in formal_by_me if row.id]),
        AuditLog.created_at >= day_start,
    ).count() if formal_by_me else 0

    admin_metrics = None
    if _is_admin() or can_manage_acting or can_manage_formal:
        active_acting_count = ActingPermission.query.filter_by(status=ACTIVE).count()
        active_formal_count = FormalDelegation.query.filter_by(status=ACTIVE).count()
        expiring_at = now + timedelta(days=7)
        expiring_count = FormalDelegation.query.filter(
            FormalDelegation.status == ACTIVE,
            FormalDelegation.end_at >= now,
            FormalDelegation.end_at <= expiring_at,
        ).count()
        missing_reference_count = FormalDelegation.query.filter(
            FormalDelegation.status == ACTIVE,
            FormalDelegation.legal_reference.is_(None),
            FormalDelegation.decision_number.is_(None),
        ).count()
        sensitive_count = AuditLog.query.filter(
            AuditLog.formal_delegation_id.isnot(None),
            AuditLog.created_at >= day_start,
            AuditLog.action_type.in_((
                "APPROVE", "CONSENT", "SIGN", "SIGNATURE", "FINANCIAL_APPROVE",
                "APPROVE_FINANCIAL", "ISSUE_DECISION", "DECIDE", "REJECT",
                "CANCEL", "CLOSE",
                "REOPEN",
            )),
        ).count()
        admin_metrics = {
            "active_acting_count": active_acting_count,
            "active_formal_count": active_formal_count,
            "expiring_count": expiring_count,
            "missing_reference_count": missing_reference_count,
            "sensitive_count": sensitive_count,
        }

    return render_template(
        "delegation/permissions.html",
        now=now,
        users=users,
        active_acting_for_me=active_acting_for_me,
        active_formal_for_me=active_formal_for_me,
        granted_by_me=granted_by_me,
        formal_by_me=formal_by_me,
        operations_for_me=operations_for_me,
        delegated_operations_by_me=delegated_operations_by_me,
        acting_operations_today=acting_operations_today,
        formal_operations_today=formal_operations_today,
        admin_metrics=admin_metrics,
        can_manage_acting=can_manage_acting,
        can_manage_formal=can_manage_formal,
        execution_context=get_execution_context(),
        action_labels=ACTION_LABELS_AR,
        action_fields=ACTING_FORM_FIELDS,
        sensitive_action_fields=SENSITIVE_ACTION_FIELDS,
        legacy_delegations_for_me=get_available_delegations(),
    )


@delegation_bp.route("/acting/create", methods=["POST"])
@delegation_bp.route("/acting-permissions/create", methods=["POST"])
@login_required
def create_acting_permission():
    principal_id = _int_form(request.form, "principal_user_id", "from_user_id") or int(current_user.id)
    acting_id = _int_form(request.form, "acting_user_id", "to_user_id")
    if not acting_id:
        flash("يرجى اختيار المنفذ بالنيابة.", "danger")
        return redirect(_next_url("delegation.permissions_dashboard"))
    if int(principal_id) == int(acting_id):
        flash("لا يمكن منح الصلاحية للمستخدم نفسه.", "danger")
        return redirect(_next_url("delegation.permissions_dashboard"))
    if not can_create_acting_permission(current_user, principal_id):
        flash("لا تملك صلاحية إنشاء هذه الصلاحية بالنيابة.", "danger")
        return redirect(_next_url("delegation.permissions_dashboard"))

    principal = db.session.get(User, principal_id)
    acting_user = db.session.get(User, acting_id)
    if not principal or not acting_user:
        flash("تعذر العثور على صاحب الصلاحية أو المنفذ.", "danger")
        return redirect(_next_url("delegation.permissions_dashboard"))

    starts_at, ends_at = _date_window(request.form)
    if not starts_at or not ends_at or ends_at < starts_at:
        flash("يرجى إدخال فترة صحيحة للصلاحية.", "danger")
        return redirect(_next_url("delegation.permissions_dashboard"))

    actions = _selected_actions(request.form)
    if not actions:
        flash("اختر إجراءً واحدًا على الأقل.", "danger")
        return redirect(_next_url("delegation.permissions_dashboard"))
    if any(normalize_action(value) in FORMAL_REQUIRED_ACTIONS for value in request.form.getlist("actions")):
        flash("الاعتماد والتوقيع وإصدار القرار لا تمنح ضمن الصلاحية التشغيلية؛ استخدم التفويض الرسمي.", "warning")
        return redirect(_next_url("delegation.permissions_dashboard"))

    values = _permission_values(actions)
    row = ActingPermission(
        principal_user_id=int(principal_id),
        acting_user_id=int(acting_id),
        module_id=(request.form.get("module_id") or request.form.get("module") or "").strip() or None,
        module_name=(request.form.get("module_name") or "").strip() or None,
        scope_type=(request.form.get("scope_type") or "ALL").strip().upper() or "ALL",
        scope_id=(request.form.get("scope_id") or "").strip() or None,
        transaction_type=(request.form.get("transaction_type") or "").strip() or None,
        request_type=(request.form.get("request_type") or "").strip() or None,
        start_at=starts_at,
        end_at=ends_at,
        status=ACTIVE,
        reason=(request.form.get("reason") or "").strip() or None,
        reference_number=(request.form.get("reference_number") or "").strip() or None,
        notes=(request.form.get("notes") or request.form.get("note") or "").strip() or None,
        created_by_id=int(current_user.id),
        **values,
    )

    try:
        db.session.add(row)
        db.session.flush()
        record_execution_audit(
            "ACTING_PERMISSION_CREATE",
            request_id=None,
            target_type="ACTING_PERMISSION",
            target_id=row.id,
            module_name=row.module_id or row.module_name,
            note=f"منح {acting_user.full_name} صلاحية تشغيلية بالنيابة عن {principal.full_name}",
        )
        actions_label = "، ".join(ACTION_LABELS_AR.get(action, action) for action in sorted(actions))
        _grant_notification(
            acting_user.id,
            f"تم منحك صلاحية للعمل بالنيابة عن {_user_label(principal)}: {actions_label}. النطاق: {row.scope_type or 'عام'}.",
            link_url=url_for("delegation.permissions_dashboard"),
        )
        db.session.commit()
        flash("تم منح الصلاحية بالنيابة بنجاح.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"تعذر منح الصلاحية بالنيابة: {exc}", "danger")
    return redirect(_next_url("delegation.permissions_dashboard"))


@delegation_bp.route("/formal/create", methods=["POST"])
@delegation_bp.route("/delegations/create", methods=["POST"])
@login_required
def create_formal_delegation():
    if not can_create_formal_delegation(current_user):
        flash("لا تملك صلاحية إنشاء تفويض رسمي.", "danger")
        return redirect(_next_url("delegation.permissions_dashboard"))

    delegator_id = _int_form(request.form, "delegator_user_id", "from_user_id") or int(current_user.id)
    delegate_id = _int_form(request.form, "delegate_user_id", "to_user_id", "acting_user_id")
    if not delegate_id or int(delegator_id) == int(delegate_id):
        flash("يرجى اختيار صاحب التفويض والمفوض إليه بشكل صحيح.", "danger")
        return redirect(_next_url("delegation.permissions_dashboard"))
    if not _is_admin() and int(delegator_id) != int(current_user.id):
        flash("لا يمكنك إنشاء تفويض باسم مستخدم آخر.", "danger")
        return redirect(_next_url("delegation.permissions_dashboard"))

    delegator = db.session.get(User, delegator_id)
    delegate = db.session.get(User, delegate_id)
    if not delegator or not delegate:
        flash("تعذر العثور على صاحب التفويض أو المفوض إليه.", "danger")
        return redirect(_next_url("delegation.permissions_dashboard"))

    # A delegate cannot silently re-delegate received authority.  The source
    # row must explicitly permit re-delegation; its type, scope and time window
    # are checked again after the submitted form is parsed below.
    redelegation_sources = []
    if not _is_admin() and int(delegator_id) == int(current_user.id):
        inbound = get_active_formal_delegations(current_user.id)
        redelegation_sources = [row for row in inbound if bool(row.allow_redelegation)]
        if inbound and not redelegation_sources:
            flash("لا يجوز إعادة تفويض صلاحية مستلمة دون نص صريح يسمح بذلك.", "danger")
            return redirect(_next_url("delegation.permissions_dashboard"))

    delegation_type = (request.form.get("delegation_type") or request.form.get("type") or "").strip()
    legal_reference = (request.form.get("legal_reference") or "").strip() or None
    decision_number = (request.form.get("decision_number") or request.form.get("reference_number") or "").strip() or None
    if not delegation_type:
        flash("يرجى اختيار نوع التفويض.", "danger")
        return redirect(_next_url("delegation.permissions_dashboard"))
    if not legal_reference and not decision_number:
        flash("المرجعية القانونية أو رقم القرار مطلوبان للتفويض الرسمي.", "danger")
        return redirect(_next_url("delegation.permissions_dashboard"))

    attachment_id = _int_form(request.form, "attachment_id")
    if attachment_id:
        attachment = db.session.get(ArchivedFile, attachment_id)
        if not attachment or bool(getattr(attachment, "is_deleted", False)) or bool(getattr(attachment, "is_final_deleted", False)):
            flash("مرفق القرار المحدد غير موجود أو غير متاح.", "danger")
            return redirect(_next_url("delegation.permissions_dashboard"))

    starts_at, ends_at = _date_window(request.form)
    if not starts_at or not ends_at or ends_at < starts_at:
        flash("يرجى إدخال فترة صحيحة للتفويض.", "danger")
        return redirect(_next_url("delegation.permissions_dashboard"))

    if redelegation_sources:
        if not any(
            _redelegation_is_within_source(
                source,
                delegation_type=delegation_type,
                module_id=(request.form.get("module_id") or request.form.get("module") or "").strip() or None,
                scope_type=(request.form.get("scope_type") or "ALL").strip().upper() or "ALL",
                scope_id=(request.form.get("scope_id") or "").strip() or None,
                transaction_type=(request.form.get("transaction_type") or "").strip() or None,
                request_type=(request.form.get("request_type") or "").strip() or None,
                starts_at=starts_at,
                ends_at=ends_at,
                allow_redelegation=bool(request.form.get("allow_redelegation")),
            )
            for source in redelegation_sources
        ):
            flash("إعادة التفويض يجب أن تبقى ضمن نوع التفويض ونطاقه ومدته الأصلية.", "danger")
            return redirect(_next_url("delegation.permissions_dashboard"))

    row = FormalDelegation(
        delegator_user_id=int(delegator_id),
        delegate_user_id=int(delegate_id),
        delegation_type=delegation_type,
        module_id=(request.form.get("module_id") or request.form.get("module") or "").strip() or None,
        module_name=(request.form.get("module_name") or "").strip() or None,
        scope_type=(request.form.get("scope_type") or "ALL").strip().upper() or "ALL",
        scope_id=(request.form.get("scope_id") or "").strip() or None,
        transaction_type=(request.form.get("transaction_type") or "").strip() or None,
        request_type=(request.form.get("request_type") or "").strip() or None,
        legal_reference=legal_reference,
        decision_number=decision_number,
        decision_date=_parse_date(request.form.get("decision_date")),
        issuing_authority=(request.form.get("issuing_authority") or "").strip() or None,
        attachment_id=attachment_id,
        start_at=starts_at,
        end_at=ends_at,
        status=ACTIVE,
        notes=(request.form.get("notes") or request.form.get("note") or "").strip() or None,
        allow_redelegation=bool(request.form.get("allow_redelegation")),
        created_by_id=int(current_user.id),
    )
    try:
        db.session.add(row)
        db.session.flush()
        record_execution_audit(
            "FORMAL_DELEGATION_CREATE",
            request_id=None,
            target_type="FORMAL_DELEGATION",
            target_id=row.id,
            module_name=row.module_id or row.module_name,
            note=f"إنشاء تفويض {delegation_type} من {delegator.full_name} إلى {delegate.full_name}",
        )
        reference = decision_number or legal_reference or f"#{row.id}"
        _grant_notification(
            delegate.id,
            f"تم منحك تفويضًا من {_user_label(delegator)} لتنفيذ {delegation_type}. المرجع: {reference}.",
            link_url=url_for("delegation.permissions_dashboard"),
            notification_type="FORMAL_DELEGATION",
        )
        db.session.commit()
        flash("تم إنشاء التفويض الرسمي بنجاح.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"تعذر إنشاء التفويض الرسمي: {exc}", "danger")
    return redirect(_next_url("delegation.permissions_dashboard"))


@delegation_bp.route("/acting/<int:permission_id>/revoke", methods=["POST"])
@delegation_bp.route("/acting-permissions/<int:permission_id>/revoke", methods=["POST"])
@login_required
def revoke_acting_permission(permission_id: int):
    row = ActingPermission.query.get_or_404(permission_id)
    if not (_is_admin() or int(row.principal_user_id) == int(current_user.id)):
        return jsonify({"error": "forbidden"}), 403
    if row.is_active:
        row.status = "REVOKED"
        row.revoked_by_id = int(current_user.id)
        row.revoked_at = datetime.utcnow()
        try:
            record_execution_audit(
                "ACTING_PERMISSION_REVOKE",
                target_type="ACTING_PERMISSION",
                target_id=row.id,
                module_name=row.module_id or row.module_name,
                note=f"إلغاء الصلاحية بالنيابة #{row.id}",
            )
            _grant_notification(
                row.acting_user_id,
                f"تم إلغاء صلاحيتك للعمل بالنيابة عن {_user_label(row.principal_user)}.",
                link_url=url_for("delegation.permissions_dashboard"),
                notification_type="DELEGATION_REVOKED",
            )
            db.session.commit()
            flash("تم إلغاء الصلاحية بالنيابة.", "success")
        except Exception as exc:
            db.session.rollback()
            flash(f"تعذر إلغاء الصلاحية: {exc}", "danger")
    return redirect(_next_url("delegation.permissions_dashboard"))


@delegation_bp.route("/formal/<int:delegation_id>/revoke", methods=["POST"])
@delegation_bp.route("/delegations/<int:delegation_id>/revoke", methods=["POST"])
@login_required
def revoke_formal_delegation(delegation_id: int):
    row = FormalDelegation.query.get_or_404(delegation_id)
    if not (_is_admin() or int(row.delegator_user_id) == int(current_user.id)):
        return jsonify({"error": "forbidden"}), 403
    if row.is_active:
        row.status = "REVOKED"
        row.revoked_by_id = int(current_user.id)
        row.revoked_at = datetime.utcnow()
        try:
            record_execution_audit(
                "FORMAL_DELEGATION_REVOKE",
                target_type="FORMAL_DELEGATION",
                target_id=row.id,
                module_name=row.module_id or row.module_name,
                note=f"إلغاء التفويض الرسمي #{row.id}",
            )
            _grant_notification(
                row.delegate_user_id,
                f"تم إلغاء التفويض رقم {row.decision_number or row.id}.",
                link_url=url_for("delegation.permissions_dashboard"),
                notification_type="FORMAL_DELEGATION_REVOKED",
            )
            db.session.commit()
            flash("تم إلغاء التفويض الرسمي.", "success")
        except Exception as exc:
            db.session.rollback()
            flash(f"تعذر إلغاء التفويض الرسمي: {exc}", "danger")
    return redirect(_next_url("delegation.permissions_dashboard"))


@delegation_bp.route("/context/acting/<int:permission_id>", methods=["POST"])
@delegation_bp.route("/select-acting/<int:permission_id>", methods=["POST"])
@login_required
def select_acting(permission_id: int):
    try:
        clear_legacy_delegation_selection()
        select_acting_permission(permission_id)
        mark_identity_choice_selected()
        flash("تم تفعيل وضع العمل بالنيابة.", "success")
    except AuthorizationError as exc:
        flash(str(exc), "danger")
    return redirect(_next_url("delegation.permissions_dashboard"))


@delegation_bp.route("/context/formal/<int:delegation_id>", methods=["POST"])
@delegation_bp.route("/select-formal/<int:delegation_id>", methods=["POST"])
@login_required
def select_formal(delegation_id: int):
    try:
        clear_legacy_delegation_selection()
        select_formal_delegation(delegation_id)
        mark_identity_choice_selected()
        flash("تم تفعيل وضع التنفيذ بموجب التفويض.", "success")
    except AuthorizationError as exc:
        flash(str(exc), "danger")
    return redirect(_next_url("delegation.permissions_dashboard"))


@delegation_bp.route("/context/clear", methods=["POST"])
@delegation_bp.route("/clear-context", methods=["POST"])
@login_required
def clear_context():
    clear_execution_context()
    clear_legacy_delegation_selection()
    mark_identity_choice_selected()
    flash("تمت العودة إلى العمل بصفتك الشخصية.", "success")
    return redirect(_next_url("delegation.permissions_dashboard"))


@delegation_bp.route("/context/self", methods=["POST"])
@login_required
def select_self_identity():
    """Confirm personal mode after the identity chooser is displayed."""
    clear_execution_context()
    clear_legacy_delegation_selection()
    mark_identity_choice_selected()
    flash("تم اختيار العمل بصفتي الشخصية.", "success")
    return redirect(_next_url("workflow.inbox"))


@delegation_bp.route("/context/legacy/<int:delegation_id>", methods=["POST"])
@login_required
def select_legacy(delegation_id: int):
    """Activate one of the backwards-compatible delegations explicitly."""
    try:
        select_legacy_delegation(delegation_id)
        flash("تم تفعيل العمل بالنيابة ضمن التفويض المحدد.", "success")
    except PermissionError as exc:
        flash(str(exc), "danger")
    return redirect(_next_url("workflow.inbox"))


@delegation_bp.route("/context", methods=["GET"])
@login_required
def context_api():
    # Load the legacy selection too; this endpoint is also used by lightweight
    # clients that do not run the application's global before-request hook.
    working_user = get_effective_user()
    context = get_execution_context()
    return jsonify({
        "actual_user_id": context.get("actual_user_id"),
        "working_user_id": getattr(working_user, "id", None),
        "acting_for_user_id": context.get("acting_for_user_id"),
        "acting_permission_id": context.get("acting_permission_id"),
        "formal_delegation_id": context.get("formal_delegation_id"),
        "legacy_delegation_id": getattr(getattr(g, "delegation", None), "id", None),
        "execution_context": context.get("execution_context", "SELF"),
    })
