from collections import defaultdict
from io import BytesIO
from datetime import date, datetime, timedelta

from flask import abort, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_

from . import portal_bp
from extensions import db
from models import (
    EmployeeFile,
    InvEmployeeRequest,
    InvEmployeeRequestAction,
    InvEmployeeRequestLine,
    InvIssueVoucher,
    InvIssueVoucherLine,
    InvItem,
    InvItemAttribute,
    InvItemCategory,
    InvReturnVoucher,
    InvReturnVoucherLine,
    InvWarehouse,
    Message,
    MessageRecipient,
    Notification,
    SystemSetting,
    User,
    UserPermission,
)
from utils.perms import perm_required
from services.official_request_forms import (
    DOCX_MIME,
    build_supply_request_docx,
    build_supply_request_pdf,
    official_form_filename,
)
from services.hr_request_workflow import secretary_general_user_ids
from utils.inventory_numbers import auto_inventory_voucher_no


ROUTE_NORMAL = "NORMAL"
ROUTE_TECH = "TECH"
ROUTE_ADMIN_MAINTENANCE = "ADMIN_MAINTENANCE"

STAGE_TECH_WAREHOUSE = "TECH_WAREHOUSE"
STAGE_TECH_DIRECTOR = "TECH_DIRECTOR"
STAGE_ADMIN_MAINTENANCE = "ADMIN_MAINTENANCE"
STAGE_ADMIN_FINANCE = "ADMIN_FINANCE"
STAGE_SECRETARY_GENERAL = "SECRETARY_GENERAL"

STAGES = {
    "HR": "مدير الشؤون البشرية (بديل)",
    "WAREHOUSE": "مدير المستودع",
    STAGE_TECH_WAREHOUSE: "مدير المستودع التكنولوجي",
    STAGE_TECH_DIRECTOR: "مدير عام الإدارة العامة للتكنولوجيا والمطبوعات",
    STAGE_ADMIN_MAINTENANCE: "مسؤول المستودع الإداري للصيانة والأثاث",
    STAGE_ADMIN_FINANCE: "مدير عام الشؤون الإدارية والمالية",
    STAGE_SECRETARY_GENERAL: "الأمين العام",
    "DONE": "مكتمل",
}

WAREHOUSE_REVIEW_STAGES = frozenset({
    "WAREHOUSE",
    "HR",
    STAGE_TECH_WAREHOUSE,
    STAGE_ADMIN_MAINTENANCE,
})

NEXT_APPROVAL_STAGE = {
    STAGE_TECH_WAREHOUSE: STAGE_TECH_DIRECTOR,
    STAGE_TECH_DIRECTOR: STAGE_ADMIN_FINANCE,
    STAGE_ADMIN_MAINTENANCE: STAGE_ADMIN_FINANCE,
}

SPECIAL_ROUTE_REQUIRED_STAGES = {
    ROUTE_TECH: (
        STAGE_TECH_WAREHOUSE,
        STAGE_TECH_DIRECTOR,
        STAGE_ADMIN_FINANCE,
    ),
    ROUTE_ADMIN_MAINTENANCE: (
        STAGE_ADMIN_MAINTENANCE,
        STAGE_ADMIN_FINANCE,
    ),
}

STAGE_ASSIGNMENT_SETTINGS = {
    STAGE_TECH_WAREHOUSE: "INVENTORY_TECH_WAREHOUSE_MANAGER_USER_ID",
    STAGE_TECH_DIRECTOR: "INVENTORY_TECH_DIRECTOR_USER_ID",
    STAGE_ADMIN_MAINTENANCE: "INVENTORY_ADMIN_MAINTENANCE_USER_ID",
    STAGE_ADMIN_FINANCE: "INVENTORY_ADMIN_FINANCE_DIRECTOR_USER_ID",
    STAGE_SECRETARY_GENERAL: "INVENTORY_SECRETARY_GENERAL_USER_ID",
}

STAGE_APPROVAL_PERMISSIONS = {
    STAGE_TECH_WAREHOUSE: "INVENTORY_TECH_WAREHOUSE_APPROVE",
    STAGE_TECH_DIRECTOR: "INVENTORY_TECH_DIRECTOR_APPROVE",
    STAGE_ADMIN_MAINTENANCE: "INVENTORY_ADMIN_MAINTENANCE_APPROVE",
    STAGE_ADMIN_FINANCE: "INVENTORY_ADMIN_FINANCE_DIRECTOR_APPROVE",
    STAGE_SECRETARY_GENERAL: "INVENTORY_SECRETARY_GENERAL_APPROVE",
}

# These three roles are mutually exclusive operational assignments.  A system
# administrator may still intervene as an administrator, but must never be
# inferred as a warehouse approver merely because ADMIN/SUPER_ADMIN has every
# permission.  Keeping the maps together also lets the settings page prevent
# accidentally assigning one account to multiple warehouse queues.
WAREHOUSE_STAGE_ASSIGNMENT_SETTINGS = {
    "WAREHOUSE": "INVENTORY_WAREHOUSE_MANAGER_USER_ID",
    STAGE_TECH_WAREHOUSE: STAGE_ASSIGNMENT_SETTINGS[STAGE_TECH_WAREHOUSE],
    STAGE_ADMIN_MAINTENANCE: STAGE_ASSIGNMENT_SETTINGS[STAGE_ADMIN_MAINTENANCE],
}

WAREHOUSE_STAGE_APPROVAL_PERMISSIONS = {
    "WAREHOUSE": "INVENTORY_REQUEST_APPROVE",
    STAGE_TECH_WAREHOUSE: STAGE_APPROVAL_PERMISSIONS[STAGE_TECH_WAREHOUSE],
    STAGE_ADMIN_MAINTENANCE: STAGE_APPROVAL_PERMISSIONS[STAGE_ADMIN_MAINTENANCE],
}

WAREHOUSE_ASSIGNMENT_FORM_STAGES = {
    "warehouse_manager_user_id": "WAREHOUSE",
    "tech_warehouse_manager_user_id": STAGE_TECH_WAREHOUSE,
    "admin_maintenance_user_id": STAGE_ADMIN_MAINTENANCE,
}

STATUS_LABELS = {
    "SUBMITTED": "قيد الاعتماد",
    "APPROVED": "معتمد ومصروف",
    "REJECTED": "مرفوض",
    "CANCELLED": "ملغى من الموظف",
    "PARTIALLY_RETURNED": "معاد جزئيا",
    "RETURNED": "معاد بالكامل",
}


def _status_label(status, approval_stage):
    if status == "SUBMITTED":
        return f"بانتظار {STAGES.get(approval_stage, approval_stage)}"
    return STATUS_LABELS.get(status, status)


def _request_status_label(row):
    return _status_label(row.status, _effective_approval_stage(row))


def _last_request_label(created_at):
    if not created_at:
        return "لم يسبق طلب الصنف"
    age_days = max(0, (datetime.utcnow().date() - created_at.date()).days)
    if age_days == 0:
        return "آخر طلب: اليوم (أقل من شهر)"
    if age_days <= 30:
        return f"آخر طلب: منذ {age_days} يومًا (أقل من شهر)"
    return f"آخر طلب: {created_at.strftime('%Y-%m-%d')} (أكثر من شهر)"


def _latest_item_request_history(requester_user_id, item_ids, *, exclude_request_id=None):
    item_ids = {int(item_id) for item_id in item_ids if item_id}
    if not item_ids:
        return {}
    query = (
        db.session.query(
            InvEmployeeRequestLine.item_id,
            InvEmployeeRequestLine.requested_qty,
            InvEmployeeRequest.id,
            InvEmployeeRequest.status,
            InvEmployeeRequest.approval_stage,
            InvEmployeeRequest.created_at,
        )
        .join(InvEmployeeRequest, InvEmployeeRequest.id == InvEmployeeRequestLine.request_id)
        .filter(InvEmployeeRequest.requester_user_id == int(requester_user_id))
        .filter(InvEmployeeRequest.status != "RETURNED")
        .filter(InvEmployeeRequestLine.item_id.in_(item_ids))
    )
    if exclude_request_id:
        query = query.filter(InvEmployeeRequest.id != int(exclude_request_id))
    rows = query.order_by(
        InvEmployeeRequest.created_at.desc(),
        InvEmployeeRequest.id.desc(),
        InvEmployeeRequestLine.id.desc(),
    ).all()
    history = {}
    for item_id, quantity, request_id, status, approval_stage, created_at in rows:
        if int(item_id) in history:
            continue
        age_days = max(0, (datetime.utcnow().date() - created_at.date()).days) if created_at else None
        history[int(item_id)] = {
            "request_id": int(request_id),
            "requested_qty": float(quantity or 0),
            "age_days": age_days,
            "within_month": age_days is not None and age_days <= 30,
            "label": _last_request_label(created_at),
            "status_label": _status_label(status, approval_stage),
        }
    return history


def _setting(key):
    row = SystemSetting.query.filter_by(key=key).first()
    return int(row.value) if row and (row.value or "").isdigit() else None


def _set_setting(key, value):
    row = SystemSetting.query.filter_by(key=key).first()
    if row:
        row.value = value
    else:
        db.session.add(SystemSetting(key=key, value=value))


def _grant_permission(user_id, key):
    if not user_id:
        return
    row = UserPermission.query.filter_by(user_id=user_id, key=key).first()
    if row:
        row.is_allowed = True
    else:
        db.session.add(UserPermission(user_id=user_id, key=key, is_allowed=True))


def _unique_user_ids(values, *, exclude_user_id=None):
    excluded = {int(exclude_user_id)} if exclude_user_id else set()
    result = []
    seen = set()
    for value in values:
        try:
            user_id = int(value)
        except (TypeError, ValueError):
            continue
        if user_id <= 0 or user_id in excluded or user_id in seen:
            continue
        if db.session.get(User, user_id) is None:
            continue
        seen.add(user_id)
        result.append(user_id)
    return result


def _direct_permission_user_ids(permission, *, exclude_user_id=None):
    """Return users explicitly granted one permission.

    Approval routing is an operational assignment, not a generic access
    check.  In particular, ``User.has_perm`` deliberately gives ADMIN and
    SUPER_ADMIN every permission, which must not turn each administrator into
    a warehouse manager or notification recipient.
    """
    permission = (permission or "").strip().upper()
    if not permission:
        return []
    user_ids = [
        user_id
        for (user_id,) in (
            db.session.query(UserPermission.user_id)
            .filter(func.upper(UserPermission.key) == permission)
            .filter(UserPermission.is_allowed.is_(True))
            .all()
        )
    ]
    return _unique_user_ids(user_ids, exclude_user_id=exclude_user_id)


def _warehouse_stage_conflict_ids(stage, *, include_legacy_permissions=False):
    """Return users assigned to the other warehouse-review queues.

    Explicit settings are the normal source of truth.  When an older setup
    has not yet saved those settings, direct permissions are a safe migration
    fallback, provided that a user is not already assigned to another
    warehouse queue.
    """
    stage = (stage or "").strip().upper()
    conflicts = set()
    for other_stage, setting_key in WAREHOUSE_STAGE_ASSIGNMENT_SETTINGS.items():
        if other_stage == stage:
            continue
        configured_user_id = _setting(setting_key)
        if configured_user_id:
            conflicts.add(configured_user_id)
        if include_legacy_permissions:
            permission = WAREHOUSE_STAGE_APPROVAL_PERMISSIONS[other_stage]
            conflicts.update(_direct_permission_user_ids(permission))
    return conflicts


def _warehouse_stage_approver_ids(stage, exclude_user_id=None):
    """Resolve exactly the operational audience of one warehouse queue."""
    stage = (stage or "").strip().upper()
    setting_key = WAREHOUSE_STAGE_ASSIGNMENT_SETTINGS.get(stage)
    permission = WAREHOUSE_STAGE_APPROVAL_PERMISSIONS.get(stage)
    if not setting_key or not permission:
        return []

    configured_user_id = _setting(setting_key)
    if configured_user_id:
        # A conflicting persisted setup should not silently send a request to
        # the wrong queue.  The administrator can correct it in the settings
        # screen, where duplicate warehouse assignments are rejected.
        if configured_user_id in _warehouse_stage_conflict_ids(stage):
            return []
        return _unique_user_ids([configured_user_id], exclude_user_id=exclude_user_id)

    # Compatibility for old installations that had only user permissions.
    # Do not use User.has_perm here: it widens the queue to every global
    # administrator.  Also do not reuse a person explicitly assigned to a
    # different warehouse-review route.
    conflict_ids = _warehouse_stage_conflict_ids(
        stage,
        include_legacy_permissions=True,
    )
    candidates = [
        user_id
        for user_id in _direct_permission_user_ids(permission)
        if user_id not in conflict_ids
    ]
    return _unique_user_ids(candidates, exclude_user_id=exclude_user_id)


def _user_has_permission_without_delegation(user, key):
    """Check another user's permission without inheriting the current actor."""
    try:
        from flask import g, has_request_context

        if not has_request_context():
            return bool(user.has_perm(key))
        marker = object()
        effective_user = getattr(g, "effective_user", marker)
        if effective_user is not marker:
            delattr(g, "effective_user")
        try:
            return bool(user.has_perm(key))
        finally:
            if effective_user is not marker:
                setattr(g, "effective_user", effective_user)
    except Exception:
        return False


def _compact_user_text(value):
    return "".join(character for character in str(value or "").upper() if character.isalnum())


def _compact_category_text(value):
    """Normalize Arabic category labels for the legacy automatic route map."""
    translation = str.maketrans({
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ى": "ي",
        "ة": "ه",
        "ؤ": "و",
        "ئ": "ي",
    })
    return _compact_user_text(value).translate(translation)


DEFAULT_CATEGORY_ROUTES = {
    _compact_category_text("أجهزة اتصالات وتوابعها"): ROUTE_TECH,
    _compact_category_text("أجهزة إلكترونية"): ROUTE_TECH,
    _compact_category_text("أجهزة حاسوب وتوابعها"): ROUTE_TECH,
    _compact_category_text("برامج حاسوب وخدمات إلكترونية"): ROUTE_TECH,
    _compact_category_text("أثاث"): ROUTE_ADMIN_MAINTENANCE,
    _compact_category_text("صيانة وإصلاحات"): ROUTE_ADMIN_MAINTENANCE,
}


# The imported catalogue uses the exact labels above, but new catalogues may
# use more specific names (for example, "ملحقات حاسوب" or "صيانة تجهيزات").
# Keep AUTO useful for those normal variations while allowing the category
# configuration to explicitly override the result whenever needed.
TECH_CATEGORY_ROUTE_KEYWORDS = tuple(
    _compact_category_text(value)
    for value in (
        "حاسوب",
        "كمبيوتر",
        "إلكترون",
        "تكنولوج",
        "برامج",
        "اتصالات",
    )
)
ADMIN_MAINTENANCE_ROUTE_KEYWORDS = tuple(
    _compact_category_text(value)
    for value in (
        "أثاث",
        "صيانة",
        "إصلاح",
    )
)


def _automatic_category_route(category_name):
    """Resolve the route for an AUTO catalogue category."""
    normalized_name = _compact_category_text(category_name)
    exact_route = DEFAULT_CATEGORY_ROUTES.get(normalized_name)
    if exact_route:
        return exact_route
    # Maintenance remains its own route even when the maintained asset is
    # technological (for example, a computer-repair category).
    if any(keyword in normalized_name for keyword in ADMIN_MAINTENANCE_ROUTE_KEYWORDS):
        return ROUTE_ADMIN_MAINTENANCE
    if any(keyword in normalized_name for keyword in TECH_CATEGORY_ROUTE_KEYWORDS):
        return ROUTE_TECH
    return ROUTE_NORMAL


def _category_request_route(category):
    """Return the route selected for one catalogue category.

    ``AUTO`` preserves the requested out-of-the-box mapping for the imported
    catalogue.  Administrators may use the category screen to explicitly mark
    any category as normal, technology, or furniture/maintenance.
    """
    configured = (getattr(category, "request_route", None) or "AUTO").strip().upper()
    if configured in {ROUTE_NORMAL, ROUTE_TECH, ROUTE_ADMIN_MAINTENANCE}:
        return configured
    return _automatic_category_route(getattr(category, "name", None))


def _route_for_requested_lines(requested_lines):
    """Resolve one safe route for a newly submitted set of request lines."""
    item_ids = {int(item_id) for item_id, _quantity in requested_lines if item_id}
    if not item_ids:
        return ROUTE_NORMAL
    items = {
        item.id: item
        for item in InvItem.query.filter(InvItem.id.in_(item_ids)).all()
    }
    routes = {
        _category_request_route(getattr(items.get(item_id), "category", None))
        for item_id in item_ids
        if items.get(item_id) is not None
    }
    routes.discard("")
    if not routes:
        return ROUTE_NORMAL
    if len(routes) > 1:
        raise ValueError(
            "لا يمكن جمع أصناف ذات مسارات اعتماد مختلفة في طلب واحد. "
            "يرجى فصل طلب التكنولوجيا، أو الأثاث/الصيانة، عن الطلب العادي."
        )
    return routes.pop()


def _initial_stage_for_route(route_type, exclude_user_id=None):
    route_type = (route_type or ROUTE_NORMAL).upper()
    if route_type == ROUTE_TECH:
        return STAGE_TECH_WAREHOUSE
    if route_type == ROUTE_ADMIN_MAINTENANCE:
        return STAGE_ADMIN_MAINTENANCE
    return _warehouse_or_hr_stage(exclude_user_id)


def _hr_fallback_approver_ids(exclude_user_id=None):
    """Resolve the HR director used when the warehouse stage has no delegate."""
    configured = _unique_user_ids(
        [_setting("INVENTORY_REQUEST_HR_FALLBACK_USER_ID")],
        exclude_user_id=exclude_user_id,
    )
    if configured:
        return configured

    users = User.query.order_by(User.id.asc()).all()
    title_markers = {
        "مديرالشؤونالبشرية",
        "مديرالمواردالبشرية",
        "رئيسالمواردالبشرية",
        "مديرالشؤونالإدارية",
        "HRDIRECTOR",
        "HUMANRESOURCESDIRECTOR",
        "HUMANRESOURCESMANAGER",
        "PERSONNELDIRECTOR",
        "PERSONNELMANAGER",
        "ADMINISTRATIVEAFFAIRSMANAGER",
    }
    title_candidates = [
        user.id
        for user in users
        if any(
            marker in _compact_user_text(getattr(user, "job_title", None))
            for marker in title_markers
        )
    ]
    if title_candidates:
        return _unique_user_ids(title_candidates, exclude_user_id=exclude_user_id)

    role_groups = (
        {"HRDIRECTOR", "HUMANRESOURCESDIRECTOR"},
        {"HRMANAGER", "HUMANRESOURCESMANAGER", "PERSONNELMANAGER"},
        {"HRADMIN", "ADMINISTRATIVEAFFAIRSMANAGER"},
        {"HR"},
    )
    for roles in role_groups:
        candidates = [
            user.id
            for user in users
            if _compact_user_text(getattr(user, "role", None)) in roles
        ]
        resolved = _unique_user_ids(candidates, exclude_user_id=exclude_user_id)
        if resolved:
            return resolved

    # Last automatic fallback: a user explicitly entrusted with HR request
    # review and visibility, without widening the route to every administrator.
    candidates = [
        user.id
        for user in users
        if user.id != exclude_user_id
        and _user_has_permission_without_delegation(user, "HR_REQUESTS_APPROVE")
        and _user_has_permission_without_delegation(user, "HR_REQUESTS_VIEW_ALL")
    ]
    return _unique_user_ids(candidates, exclude_user_id=exclude_user_id)


def _warehouse_approver_ids(exclude_user_id=None):
    """Return warehouse approvers, excluding the requester to prevent self-approval."""
    return _warehouse_stage_approver_ids("WAREHOUSE", exclude_user_id)


def _special_stage_approver_ids(stage, exclude_user_id=None):
    """Resolve one dedicated special-route approval audience.

    The selected account in settings is authoritative.  Legacy direct
    permission assignments remain supported, but generic administrator access
    is never used to infer a named business approver.
    """
    stage = (stage or "").strip().upper()
    if stage in WAREHOUSE_STAGE_ASSIGNMENT_SETTINGS:
        return _warehouse_stage_approver_ids(stage, exclude_user_id)

    configured = _setting(STAGE_ASSIGNMENT_SETTINGS.get(stage, ""))
    if configured:
        resolved = _unique_user_ids([configured], exclude_user_id=exclude_user_id)
        if resolved:
            return resolved

    permission = STAGE_APPROVAL_PERMISSIONS.get(stage)
    candidates = _direct_permission_user_ids(permission, exclude_user_id=exclude_user_id)
    if stage == STAGE_SECRETARY_GENERAL:
        candidates.extend(secretary_general_user_ids())
    return _unique_user_ids(candidates, exclude_user_id=exclude_user_id)


def _missing_route_stages(route_type, exclude_user_id=None):
    return [
        stage
        for stage in SPECIAL_ROUTE_REQUIRED_STAGES.get(route_type, ())
        if not _special_stage_approver_ids(stage, exclude_user_id)
    ]


def _warehouse_or_hr_stage(exclude_user_id=None):
    return "WAREHOUSE" if _warehouse_approver_ids(exclude_user_id) else "HR"


def _is_system_admin(user):
    try:
        return bool(user.has_role("SUPER_ADMIN") or user.has_role("SUPERADMIN") or user.has_role("ADMIN"))
    except Exception:
        return (getattr(user, "role", "") or "").upper().replace("_", "") in {"SUPERADMIN", "ADMIN"}


def _effective_approval_stage(row):
    """Resolve the active stage while bypassing retired manager-stage requests.

    New employee material requests go straight to the warehouse (or the HR
    fallback when the requester is the warehouse approver). Older pending
    requests may still carry the former ``MANAGER`` value, so treat them as
    being at their next real approver rather than leaving them stranded.
    """
    stage = (getattr(row, "approval_stage", None) or "").strip().upper()
    if stage == "MANAGER":
        return _warehouse_or_hr_stage(getattr(row, "requester_user_id", None))
    return stage


def _stage_approver_ids(row, stage):
    """Return the responsible users for one named materials-request stage."""
    stage = (stage or "").strip().upper()
    requester_user_id = getattr(row, "requester_user_id", None)
    if stage == "WAREHOUSE":
        return _warehouse_approver_ids(requester_user_id)
    if stage == "HR":
        return _hr_fallback_approver_ids(requester_user_id)
    if stage in STAGE_APPROVAL_PERMISSIONS:
        return _special_stage_approver_ids(stage, requester_user_id)
    return []


def _stage_responsible_labels(row):
    """Build role-to-person labels for the request's action history."""
    approver_ids_by_stage = {
        stage: _stage_approver_ids(row, stage)
        for stage in ("WAREHOUSE", "HR", *STAGE_APPROVAL_PERMISSIONS)
    }
    all_ids = {
        user_id
        for user_ids in approver_ids_by_stage.values()
        for user_id in user_ids
    }
    users_by_id = {
        user.id: user
        for user in User.query.filter(User.id.in_(all_ids)).all()
    } if all_ids else {}

    labels = {}
    for stage, user_ids in approver_ids_by_stage.items():
        names = []
        for user_id in user_ids:
            user = users_by_id.get(user_id)
            if not user:
                continue
            name = (
                getattr(user, "full_name", None)
                or getattr(user, "name", None)
                or getattr(user, "email", None)
                or str(user.id)
            )
            if name:
                names.append(str(name).strip())
        if names:
            labels[stage] = "، ".join(names)
    return labels


def _recipient_ids(row):
    return _stage_approver_ids(row, _effective_approval_stage(row))


def _can_process(row):
    if row.status != "SUBMITTED":
        return False
    if _is_system_admin(current_user):
        return True
    return current_user.id in _stage_approver_ids(row, _effective_approval_stage(row))


def _can_manage():
    configured_ids = {
        _setting("INVENTORY_WAREHOUSE_MANAGER_USER_ID"),
        *(_setting(setting_key) for setting_key in STAGE_ASSIGNMENT_SETTINGS.values()),
    }
    special_permissions = tuple(STAGE_APPROVAL_PERMISSIONS.values())
    return (
        _is_system_admin(current_user)
        or current_user.id in configured_ids
        or current_user.id in _hr_fallback_approver_ids()
        or current_user.has_perm("INVENTORY_REQUEST_APPROVE")
        or any(current_user.has_perm(permission) for permission in special_permissions)
        or current_user.id in _special_stage_approver_ids(STAGE_SECRETARY_GENERAL)
        or current_user.has_perm("STORE_MANAGE")
    )


def _can_manage_catalog():
    return current_user.has_perm("INVENTORY_REQUEST_APPROVE") or current_user.has_perm("STORE_MANAGE")


def _can_view(row):
    return row.requester_user_id == current_user.id or _can_manage() or _can_process(row) or any(
        action.actor_user_id == current_user.id for action in row.actions
    )


def _action_for_stage(row, stage, *, after=None):
    return next(
        (
            action for action in sorted(row.actions, key=lambda value: value.created_at or datetime.min, reverse=True)
            if action.stage == stage and action.action == "APPROVED" and (after is None or action.created_at >= after)
        ),
        None,
    )


def _supply_form_payload(row):
    employee = EmployeeFile.query.get(row.requester_user_id)
    last_update = max(
        (action.created_at for action in row.actions if action.action == "UPDATED" and action.created_at),
        default=None,
    )
    manager_action = _action_for_stage(row, "MANAGER", after=last_update)
    warehouse_action = (
        _action_for_stage(row, "WAREHOUSE", after=last_update)
        or _action_for_stage(row, "HR", after=last_update)
    )
    created_at = row.created_at or datetime.utcnow()
    return {
        "request_no": str(row.id),
        "request_date": created_at.strftime("%Y/%m/%d"),
        "organization": (
            getattr(getattr(employee, "organization", None), "name_ar", None)
            or getattr(getattr(employee, "organization", None), "name", None)
            or "-"
        ),
        "directorate": (
            getattr(getattr(employee, "directorate", None), "name_ar", None)
            or getattr(getattr(employee, "directorate", None), "name", None)
            or "-"
        ),
        "requester_name": row.requester.full_name or row.requester.name or row.requester.email,
        "requester_date": created_at.strftime("%Y/%m/%d"),
        "manager_name": (manager_action.actor.full_name if manager_action and manager_action.actor else ""),
        "manager_note": manager_action.note if manager_action else "",
        "warehouse_name": (warehouse_action.actor.full_name if warehouse_action and warehouse_action.actor else ""),
        "warehouse_note": warehouse_action.note if warehouse_action else "",
        "lines": [
            {
                "item": line.item.label if line.item else "-",
                "unit": (line.item.unit if line.item else "") or "-",
                "quantity": f"{float(line.requested_qty or 0):g}",
            }
            for line in row.lines
        ],
    }


def _notify(row, recipient_ids, text):
    recipient_ids = sorted({user_id for user_id in recipient_ids if user_id and user_id != current_user.id})
    if not recipient_ids:
        return
    link = url_for("portal.inventory_employee_request_view", request_id=row.id)
    for user_id in recipient_ids:
        db.session.add(Notification(
            user_id=user_id,
            message=text,
            type="INFO",
            source="portal",
            link_url=link,
            is_read=False,
            created_at=datetime.utcnow(),
        ))
    message = Message(
        sender_id=current_user.id,
        subject=f"طلب مواد #{row.id}",
        body=f"{text}\n{link}",
        target_kind="USER",
        target_id=recipient_ids[0],
        created_at=datetime.utcnow(),
        is_system_generated=True,
    )
    db.session.add(message)
    db.session.flush()
    db.session.add_all([
        MessageRecipient(message_id=message.id, recipient_user_id=user_id)
        for user_id in recipient_ids
    ])


def _inventory_balances(*, warehouse_ids=None, item_ids=None):
    from .routes import _inv_build_balances

    return _inv_build_balances(warehouse_ids=warehouse_ids, item_ids=item_ids)


def _catalog_context(*, include_items=True, item_ids=None):
    selected_item_ids = None
    if item_ids is not None:
        selected_item_ids = set()
        for item_id in item_ids:
            try:
                item_id = int(item_id)
            except (TypeError, ValueError):
                continue
            if item_id > 0:
                selected_item_ids.add(item_id)

    item_query = InvItem.query.filter(InvItem.is_active.is_(True)).order_by(InvItem.name.asc())
    # The employee request screen resolves items through the small remote
    # lookup.  Do not render the complete catalogue three times in its form.
    if not include_items:
        items = []
    elif selected_item_ids is None:
        items = item_query.all()
    elif selected_item_ids:
        items = item_query.filter(InvItem.id.in_(selected_item_ids)).all()
    else:
        items = []
    categories = InvItemCategory.query.filter(InvItemCategory.is_active.is_(True)).order_by(InvItemCategory.name.asc()).all()
    warehouses = InvWarehouse.query.filter(InvWarehouse.is_active.is_(True)).order_by(InvWarehouse.name.asc()).all()
    balances = _inventory_balances(item_ids=selected_item_ids) if include_items else {}
    item_totals = defaultdict(float)
    for (_warehouse_id, item_id), quantity in balances.items():
        item_totals[item_id] += float(quantity or 0)
    warehouse_balances = {
        f"{warehouse_id}:{item_id}": float(quantity or 0)
        for (warehouse_id, item_id), quantity in balances.items()
    }
    return items, categories, warehouses, dict(item_totals), warehouse_balances


def _parse_requested_lines():
    item_ids = request.form.getlist("item_id")
    quantities = request.form.getlist("requested_qty")
    requested = defaultdict(float)
    for item_id_raw, quantity_raw in zip(item_ids, quantities):
        if not (item_id_raw or "").isdigit():
            continue
        try:
            quantity = float(quantity_raw)
        except (TypeError, ValueError):
            continue
        if quantity > 0:
            requested[int(item_id_raw)] += quantity
    active_ids = {
        item.id for item in InvItem.query.filter(InvItem.id.in_(requested.keys()), InvItem.is_active.is_(True)).all()
    } if requested else set()
    return [(item_id, quantity) for item_id, quantity in requested.items() if item_id in active_ids]


def _request_summary(lines):
    return "، ".join(
        f"{line.requested_qty:g} {line.item.unit or 'وحدة'} من {line.item.name}"
        for line in lines
    )


def _replace_lines(row, requested_lines):
    row.lines.clear()
    db.session.flush()
    for item_id, quantity in requested_lines:
        row.lines.append(InvEmployeeRequestLine(item_id=item_id, requested_qty=quantity))
    db.session.flush()
    row.items_text = _request_summary(row.lines)


def _route_configuration_error(route_type, requester_user_id):
    """Describe any special-route roles that must be configured first."""
    missing = _missing_route_stages(route_type, exclude_user_id=requester_user_id)
    if not missing:
        return None
    return "لا يمكن إرسال الطلب قبل تعيين: " + "، ".join(
        STAGES.get(stage, stage) for stage in missing
    ) + "."


def _issue_allocation_error(allocations):
    """Validate per-line issue allocations with one set-wise stock lookup.

    Each allocation is ``(line, warehouse_id, approved_qty)``.  Grouping the
    quantities before calculating balances avoids a balance query per selected
    warehouse, even when a request is distributed across many warehouses.
    """
    requested_by_stock = defaultdict(float)
    item_labels = {}
    warehouse_ids = set()
    for line, warehouse_id, approved_qty in allocations:
        try:
            warehouse_id = int(warehouse_id)
        except (TypeError, ValueError):
            return "مستودع الصرف المختار غير صالح."
        if warehouse_id <= 0:
            return "مستودع الصرف المختار غير صالح."
        quantity = float(approved_qty or 0)
        if quantity <= 0:
            continue
        item_id = int(line.item_id)
        requested_by_stock[(warehouse_id, item_id)] += quantity
        warehouse_ids.add(warehouse_id)
        item_labels.setdefault(
            item_id,
            line.item.label if line.item else str(item_id),
        )

    if not requested_by_stock:
        return None

    warehouse_ids = tuple(sorted(warehouse_ids))
    warehouses = {
        warehouse.id: warehouse
        for warehouse in InvWarehouse.query.filter(
            InvWarehouse.id.in_(warehouse_ids),
            InvWarehouse.is_active.is_(True),
        ).all()
    }
    if len(warehouses) != len(warehouse_ids):
        return "مستودع الصرف المختار غير صالح."

    balances = _inventory_balances(
        warehouse_ids=warehouse_ids,
        item_ids=tuple(sorted({item_id for _warehouse_id, item_id in requested_by_stock})),
    )
    errors = []
    for (warehouse_id, item_id), quantity in sorted(requested_by_stock.items()):
        available = float(balances.get((warehouse_id, item_id), 0) or 0)
        if quantity > available:
            warehouse_label = warehouses[warehouse_id].label
            errors.append(
                f"{item_labels[item_id]} ({warehouse_label}): "
                f"المطلوب {quantity:g} والمتاح {available:g}"
            )
    return "الرصيد غير كافٍ: " + "؛ ".join(errors) if errors else None


def _prepared_issue_error(row):
    """Validate the per-line warehouse and quantity choices already saved."""
    allocations = []
    total_approved = 0.0
    for line in row.lines:
        approved_qty = float(line.approved_qty or 0)
        if approved_qty < 0 or approved_qty > float(line.requested_qty or 0):
            return f"الكمية المعتمدة للصنف {line.item.name if line.item else line.item_id} غير صحيحة."
        if approved_qty <= 0:
            continue
        if not line.warehouse_id:
            return "لم يتم اختيار مستودع صرف لكل صنف معتمد."
        allocations.append((line, line.warehouse_id, approved_qty))
        total_approved += approved_qty
    if total_approved <= 0:
        return "اعتمد كمية موجبة لصنف واحد على الأقل."
    return _issue_allocation_error(allocations)


def _prepare_issue_from_warehouse_review(row):
    """Store a warehouse and approved quantity independently for each line."""
    allocations = []
    selected_values = []
    total_approved = 0.0
    # Preserve compatibility with an already deployed form or integration that
    # still sends a single warehouse_id. The current screen sends one value per
    # line and never relies on this fallback.
    legacy_warehouse_id = (request.form.get("warehouse_id") or "").strip()

    for line in row.lines:
        try:
            approved_qty = float(request.form.get(f"approved_qty_{line.id}") or 0)
        except (TypeError, ValueError):
            approved_qty = 0
        if approved_qty < 0 or approved_qty > float(line.requested_qty or 0):
            raise ValueError(
                f"الكمية المعتمدة للصنف {line.item.name if line.item else line.item_id} غير صحيحة."
            )

        warehouse_id = None
        if approved_qty > 0:
            warehouse_id_raw = (
                request.form.get(f"warehouse_id_{line.id}") or legacy_warehouse_id
            ).strip()
            if not warehouse_id_raw.isdigit():
                raise ValueError(
                    f"اختر مستودع صرف للصنف {line.item.name if line.item else line.item_id}."
                )
            warehouse_id = int(warehouse_id_raw)
            allocations.append((line, warehouse_id, approved_qty))
            total_approved += approved_qty
        selected_values.append((line, approved_qty, warehouse_id))

    if total_approved <= 0:
        raise ValueError("اعتمد كمية موجبة لصنف واحد على الأقل.")
    allocation_error = _issue_allocation_error(allocations)
    if allocation_error:
        raise ValueError(allocation_error)

    for line, approved_qty, warehouse_id in selected_values:
        line.approved_qty = approved_qty
        # A rejected/zero-quantity line has no source warehouse to preserve.
        line.warehouse_id = warehouse_id


def _returned_quantities(line_ids):
    """Return quantities already linked back to each employee-request line."""
    line_ids = {int(line_id) for line_id in line_ids if line_id}
    if not line_ids:
        return {}
    rows = (
        db.session.query(
            InvReturnVoucherLine.source_request_line_id,
            func.sum(InvReturnVoucherLine.qty),
        )
        .filter(InvReturnVoucherLine.source_request_line_id.in_(line_ids))
        .group_by(InvReturnVoucherLine.source_request_line_id)
        .all()
    )
    return {int(line_id): float(quantity or 0) for line_id, quantity in rows if line_id}


def _request_returnable_lines(row):
    """Return (line, returned, remaining) tuples for an issued request."""
    returned = _returned_quantities([line.id for line in row.lines])
    result = []
    for line in row.lines:
        approved = float(line.approved_qty or 0)
        already_returned = min(approved, float(returned.get(line.id, 0) or 0))
        remaining = max(0.0, approved - already_returned)
        if remaining > 1e-9 and line.warehouse_id:
            result.append((line, already_returned, remaining))
    return result


def _create_issue_vouchers(row, *, validated=False):
    if not validated:
        prepared_error = _prepared_issue_error(row)
        if prepared_error:
            raise ValueError(prepared_error)
    grouped = defaultdict(list)
    for line in row.lines:
        if line.warehouse_id and float(line.approved_qty or 0) > 0:
            grouped[line.warehouse_id].append(line)
    for warehouse_id, lines in grouped.items():
        voucher = InvIssueVoucher(
            issue_kind="EMPLOYEE",
            voucher_no="",
            voucher_date=date.today().isoformat(),
            from_warehouse_id=warehouse_id,
            to_room_name=row.requester.full_name,
            note=f"صرف آلي مقابل طلب مواد الموظف #{row.id}: {row.purpose}",
            created_by_id=current_user.id,
        )
        db.session.add(voucher)
        db.session.flush()
        voucher.voucher_no = auto_inventory_voucher_no(
            "employee",
            voucher.voucher_date,
            voucher.id,
        )
        for line in lines:
            db.session.add(InvIssueVoucherLine(
                voucher_id=voucher.id,
                item_id=line.item_id,
                qty=line.approved_qty,
                details=f"طلب مواد الموظف #{row.id}",
            ))
            line.issue_voucher_id = voucher.id


@portal_bp.route("/admin/inventory-request-settings", methods=["GET", "POST"])
@login_required
@perm_required("PORTAL_ADMIN_PERMISSIONS_MANAGE")
def inventory_request_settings():
    assignment_fields = (
        (
            "warehouse_manager_user_id",
            "INVENTORY_WAREHOUSE_MANAGER_USER_ID",
            "INVENTORY_REQUEST_APPROVE",
            "مدير المستودع",
        ),
        (
            "tech_warehouse_manager_user_id",
            "INVENTORY_TECH_WAREHOUSE_MANAGER_USER_ID",
            "INVENTORY_TECH_WAREHOUSE_APPROVE",
            "مدير المستودع التكنولوجي",
        ),
        (
            "admin_maintenance_user_id",
            "INVENTORY_ADMIN_MAINTENANCE_USER_ID",
            "INVENTORY_ADMIN_MAINTENANCE_APPROVE",
            "مسؤول المستودع الإداري للصيانة والأثاث",
        ),
        (
            "tech_director_user_id",
            "INVENTORY_TECH_DIRECTOR_USER_ID",
            "INVENTORY_TECH_DIRECTOR_APPROVE",
            "مدير عام الإدارة العامة للتكنولوجيا والمطبوعات",
        ),
        (
            "admin_finance_director_user_id",
            "INVENTORY_ADMIN_FINANCE_DIRECTOR_USER_ID",
            "INVENTORY_ADMIN_FINANCE_DIRECTOR_APPROVE",
            "مدير عام الشؤون الإدارية والمالية",
        ),
        (
            "secretary_general_user_id",
            "INVENTORY_SECRETARY_GENERAL_USER_ID",
            "INVENTORY_SECRETARY_GENERAL_APPROVE",
            "الأمين العام",
        ),
    )
    users = User.query.order_by(User.name.asc(), User.email.asc()).all()
    valid_user_ids = {str(user.id) for user in users}
    if request.method == "POST":
        hr_fallback_user_id = request.form.get("hr_fallback_user_id") or ""
        if hr_fallback_user_id and hr_fallback_user_id not in valid_user_ids:
            flash("اختر مستخدمًا صالحًا لمدير الشؤون البشرية البديل.", "warning")
            return redirect(url_for("portal.inventory_request_settings"))
        values = {}
        for field_name, setting_key, _permission, label in assignment_fields:
            value = request.form.get(field_name) or ""
            if value and value not in valid_user_ids:
                flash(f"اختر مستخدمًا صالحًا لـ {label}.", "warning")
                return redirect(url_for("portal.inventory_request_settings"))
            values[field_name] = value

        warehouse_assignment_ids = [
            values[field_name]
            for field_name in WAREHOUSE_ASSIGNMENT_FORM_STAGES
            if values.get(field_name)
        ]
        if len(warehouse_assignment_ids) != len(set(warehouse_assignment_ids)):
            flash(
                "لا يجوز تعيين الحساب نفسه للمستودع العادي أو التكنولوجي أو الصيانة/الأثاث. اختر مسؤولاً مختلفاً لكل مسار.",
                "warning",
            )
            return redirect(url_for("portal.inventory_request_settings"))

        for field_name, setting_key, _permission, _label in assignment_fields:
            _set_setting(setting_key, values[field_name])
        warehouse_manager_id = values["warehouse_manager_user_id"]
        _set_setting("INVENTORY_REQUEST_HR_FALLBACK_USER_ID", hr_fallback_user_id)
        for field_name, _setting_key, permission, _label in assignment_fields:
            user_id = values[field_name]
            _grant_permission(int(user_id) if user_id.isdigit() else None, permission)
        _grant_permission(int(warehouse_manager_id) if warehouse_manager_id.isdigit() else None, "PORTAL_REPORTS_READ")
        db.session.commit()
        flash("تم حفظ مسؤولي مسارات اعتماد طلبات المواد.", "success")
        return redirect(url_for("portal.inventory_request_settings"))

    warehouse_assignment_ids = {
        stage: _setting(setting_key)
        for stage, setting_key in WAREHOUSE_STAGE_ASSIGNMENT_SETTINGS.items()
    }

    def _warehouse_assignment_candidates(stage):
        assigned_elsewhere = {
            user_id
            for other_stage, user_id in warehouse_assignment_ids.items()
            if other_stage != stage and user_id
        }
        return [user for user in users if user.id not in assigned_elsewhere]

    assigned_warehouse_user_ids = [
        user_id for user_id in warehouse_assignment_ids.values() if user_id
    ]
    return render_template(
        "portal/inventory/request_settings.html",
        users=users,
        warehouse_manager_id=_setting("INVENTORY_WAREHOUSE_MANAGER_USER_ID"),
        hr_fallback_user_id=_setting("INVENTORY_REQUEST_HR_FALLBACK_USER_ID"),
        tech_warehouse_manager_user_id=_setting("INVENTORY_TECH_WAREHOUSE_MANAGER_USER_ID"),
        admin_maintenance_user_id=_setting("INVENTORY_ADMIN_MAINTENANCE_USER_ID"),
        tech_director_user_id=_setting("INVENTORY_TECH_DIRECTOR_USER_ID"),
        admin_finance_director_user_id=_setting("INVENTORY_ADMIN_FINANCE_DIRECTOR_USER_ID"),
        secretary_general_user_id=_setting("INVENTORY_SECRETARY_GENERAL_USER_ID"),
        warehouse_manager_users=_warehouse_assignment_candidates("WAREHOUSE"),
        tech_warehouse_manager_users=_warehouse_assignment_candidates(STAGE_TECH_WAREHOUSE),
        admin_maintenance_users=_warehouse_assignment_candidates(STAGE_ADMIN_MAINTENANCE),
        warehouse_assignment_has_conflict=(
            len(assigned_warehouse_user_ids) != len(set(assigned_warehouse_user_ids))
        ),
    )


def _employee_request_filter_values():
    """Read and validate the GET filters shared by request-list pages."""
    status = (request.args.get("status") or "").strip().upper()
    stage = (request.args.get("stage") or "").strip().upper()
    user_id = (request.args.get("user_id") or "").strip()
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()

    if status not in STATUS_LABELS:
        status = ""
    if stage not in STAGES:
        stage = ""
    if not user_id.isdigit():
        user_id = ""

    for value_name, value in (("date_from", date_from), ("date_to", date_to)):
        try:
            date.fromisoformat(value) if value else None
        except ValueError:
            if value_name == "date_from":
                date_from = ""
            else:
                date_to = ""

    return {
        "q": (request.args.get("q") or "").strip()[:120],
        "status": status,
        "stage": stage,
        "user_id": user_id,
        "date_from": date_from,
        "date_to": date_to,
    }


def _apply_employee_request_filters(query, filters, *, can_filter_by_user):
    """Apply safe list filters without widening the caller's visibility scope."""
    status = filters.get("status") or ""
    stage = filters.get("stage") or ""
    user_id = filters.get("user_id") or ""
    query_text = filters.get("q") or ""
    date_from = filters.get("date_from") or ""
    date_to = filters.get("date_to") or ""

    if status:
        query = query.filter(InvEmployeeRequest.status == status)
    if stage == "WAREHOUSE":
        # Pending records created before the manager stage was retired have
        # the same effective warehouse/HR target.
        query = query.filter(InvEmployeeRequest.approval_stage.in_(("WAREHOUSE", "MANAGER")))
    elif stage:
        query = query.filter(InvEmployeeRequest.approval_stage == stage)
    if can_filter_by_user and user_id:
        query = query.filter(InvEmployeeRequest.requester_user_id == int(user_id))
    if query_text:
        pattern = f"%{query_text}%"
        text_filters = [
            InvEmployeeRequest.items_text.ilike(pattern),
            InvEmployeeRequest.purpose.ilike(pattern),
            InvEmployeeRequest.note.ilike(pattern),
            InvEmployeeRequest.requester.has(or_(
                User.name.ilike(pattern),
                User.email.ilike(pattern),
                User.job_title.ilike(pattern),
            )),
            InvEmployeeRequest.lines.any(
                InvEmployeeRequestLine.item.has(or_(
                    InvItem.name.ilike(pattern),
                    InvItem.code.ilike(pattern),
                    InvItem.variant.ilike(pattern),
                ))
            ),
        ]
        if query_text.isdigit():
            text_filters.append(InvEmployeeRequest.id == int(query_text))
        query = query.filter(or_(*text_filters))
    if date_from:
        query = query.filter(
            InvEmployeeRequest.created_at >= datetime.combine(date.fromisoformat(date_from), datetime.min.time())
        )
    if date_to:
        query = query.filter(
            InvEmployeeRequest.created_at < datetime.combine(
                date.fromisoformat(date_to) + timedelta(days=1),
                datetime.min.time(),
            )
        )
    return query


def _employee_request_filter_users(query):
    requester_ids = [
        requester_id
        for (requester_id,) in query.with_entities(InvEmployeeRequest.requester_user_id).distinct().all()
        if requester_id
    ]
    if not requester_ids:
        return []
    return (
        User.query
        .filter(User.id.in_(requester_ids))
        .order_by(User.name.asc(), User.email.asc())
        .all()
    )


@portal_bp.route("/inventory/employee-requests")
@login_required
def inventory_employee_requests():
    can_manage = _can_manage()
    query = InvEmployeeRequest.query
    if not can_manage:
        query = query.filter_by(requester_user_id=current_user.id)
    requester_users = _employee_request_filter_users(query) if can_manage else []
    filters = _employee_request_filter_values()
    rows = (
        _apply_employee_request_filters(query, filters, can_filter_by_user=can_manage)
        .order_by(InvEmployeeRequest.created_at.desc(), InvEmployeeRequest.id.desc())
        .all()
    )
    pending_count = sum(1 for row in rows if _can_process(row))
    return render_template(
        "portal/inventory/employee_requests.html",
        rows=rows,
        stages=STAGES,
        status_options=STATUS_LABELS,
        requester_users=requester_users,
        filters=filters,
        request_status_labels={row.id: _request_status_label(row) for row in rows},
        can_manage=can_manage,
        can_manage_catalog=_can_manage_catalog(),
        pending_count=pending_count,
    )


@portal_bp.route("/inventory/employee-requests/tasks")
@login_required
def inventory_employee_request_tasks():
    can_manage = _can_manage()
    filters = _employee_request_filter_values()
    filters["status"] = "SUBMITTED"
    query = _apply_employee_request_filters(
        InvEmployeeRequest.query.filter_by(status="SUBMITTED"),
        filters,
        can_filter_by_user=can_manage,
    )
    rows = [
        row for row in query.order_by(InvEmployeeRequest.created_at.desc(), InvEmployeeRequest.id.desc()).all()
        if _can_process(row)
    ]
    requester_users = _employee_request_filter_users(InvEmployeeRequest.query) if can_manage else []
    return render_template(
        "portal/inventory/employee_requests.html",
        rows=rows,
        stages=STAGES,
        status_options=STATUS_LABELS,
        requester_users=requester_users,
        filters=filters,
        request_status_labels={row.id: _request_status_label(row) for row in rows},
        can_manage=can_manage,
        can_manage_catalog=_can_manage_catalog(),
        pending_count=len(rows),
        tasks=True,
    )


@portal_bp.route("/inventory/employee-requests/items/search.json")
@login_required
def inventory_employee_request_items_search():
    """Return request catalogue choices with this employee's latest request."""
    search = (request.args.get("q") or "").strip()
    category_id = (request.args.get("category_id") or "").strip()
    query = InvItem.query.filter(InvItem.is_active.is_(True))
    if category_id.isdigit():
        query = query.filter(InvItem.category_id == int(category_id))
    if search:
        like = f"%{search}%"
        query = query.filter(or_(
            InvItem.code.ilike(like),
            InvItem.name.ilike(like),
            InvItem.variant.ilike(like),
            InvItem.attributes.any(or_(
                InvItemAttribute.name.ilike(like),
                InvItemAttribute.value.ilike(like),
            )),
        ))
    items = query.order_by(InvItem.code.asc(), InvItem.name.asc(), InvItem.id.asc()).limit(60).all()
    history = _latest_item_request_history(current_user.id, [item.id for item in items])
    return jsonify({
        "items": [
            {
                "id": item.id,
                "label": item.label,
                "code": item.code or "",
                "unit": item.unit or "",
                "category": item.category.name if item.category else "",
                "last_request": history.get(item.id),
            }
            for item in items
        ]
    })


@portal_bp.route("/inventory/employee-requests/new", methods=["GET", "POST"])
@login_required
def inventory_employee_request_new():
    items, categories, warehouses, item_totals, warehouse_balances = _catalog_context(include_items=False)
    catalog_has_items = InvItem.query.filter(InvItem.is_active.is_(True)).limit(1).first() is not None
    if request.method == "POST":
        requested_lines = _parse_requested_lines()
        purpose = (request.form.get("purpose") or "").strip()
        if not requested_lines or not purpose:
            flash("اختر مادة واحدة على الأقل وأدخل الكمية وسبب الطلب.", "danger")
            return render_template(
                "portal/inventory/employee_request_form.html",
                item=None,
                items=items,
                categories=categories,
                item_totals=item_totals,
                catalog_has_items=catalog_has_items,
                can_manage_catalog=_can_manage_catalog(),
            )
        try:
            route_type = _route_for_requested_lines(requested_lines)
        except ValueError as exc:
            flash(str(exc), "danger")
            return render_template(
                "portal/inventory/employee_request_form.html",
                item=None,
                items=items,
                categories=categories,
                item_totals=item_totals,
                catalog_has_items=catalog_has_items,
                can_manage_catalog=_can_manage_catalog(),
            )
        configuration_error = _route_configuration_error(route_type, current_user.id)
        if configuration_error:
            flash(configuration_error, "danger")
            return render_template(
                "portal/inventory/employee_request_form.html",
                item=None,
                items=items,
                categories=categories,
                item_totals=item_totals,
                catalog_has_items=catalog_has_items,
                can_manage_catalog=_can_manage_catalog(),
            )
        approval_stage = _initial_stage_for_route(route_type, current_user.id)
        row = InvEmployeeRequest(
            requester_user_id=current_user.id,
            items_text="",
            purpose=purpose,
            note=(request.form.get("note") or "").strip() or None,
            route_type=route_type,
            approval_stage=approval_stage,
        )
        db.session.add(row)
        db.session.flush()
        _replace_lines(row, requested_lines)
        db.session.add(InvEmployeeRequestAction(
            request_id=row.id,
            stage=row.approval_stage,
            action="SUBMITTED",
            actor_user_id=current_user.id,
            note="تم إرسال طلب المواد للاعتماد.",
        ))
        _notify(row, _recipient_ids(row), f"طلب مواد #{row.id} بانتظار متابعتك لدى {STAGES.get(row.approval_stage, row.approval_stage)}.")
        db.session.commit()
        flash("تم إرسال طلب المواد للاعتماد.", "success")
        return redirect(url_for("portal.inventory_employee_request_view", request_id=row.id))
    return render_template(
        "portal/inventory/employee_request_form.html",
        item=None,
        items=items,
        categories=categories,
        item_totals=item_totals,
        catalog_has_items=catalog_has_items,
        can_manage_catalog=_can_manage_catalog(),
    )


@portal_bp.route("/inventory/employee-requests/<int:request_id>", methods=["GET", "POST"])
@login_required
def inventory_employee_request_view(request_id):
    row = InvEmployeeRequest.query.get_or_404(request_id)
    if not _can_view(row):
        abort(403)
    # The employee owns the requested quantities. Only the requester may
    # alter a pending request; warehouse/HR approvers record their decision.
    can_edit = row.status == "SUBMITTED" and row.requester_user_id == current_user.id
    request_item_ids = [line.item_id for line in row.lines if line.item_id]
    # A request view only needs its own items' balances.  Loading every item
    # and every historic movement here made ordinary approval pages slower as
    # the warehouse catalogue grew.
    items, categories, warehouses, item_totals, warehouse_balances = _catalog_context(
        item_ids=request_item_ids,
    )
    previous_requests = _latest_item_request_history(
        row.requester_user_id,
        request_item_ids,
        exclude_request_id=row.id,
    )
    returnable_lines = (
        _request_returnable_lines(row)
        if row.status in {"APPROVED", "PARTIALLY_RETURNED"}
        else []
    )
    if request.method == "POST":
        if not can_edit:
            abort(403)
        requested_lines = _parse_requested_lines()
        purpose = (request.form.get("purpose") or "").strip()
        if not requested_lines or not purpose:
            flash("يجب أن يحتوي الطلب على مادة واحدة على الأقل وسبب واضح.", "danger")
            return redirect(request.url)
        old_signature = sorted((line.item_id, float(line.requested_qty)) for line in row.lines)
        new_signature = sorted((item_id, float(quantity)) for item_id, quantity in requested_lines)
        row.purpose = purpose
        row.note = (request.form.get("note") or "").strip() or None
        if old_signature != new_signature:
            try:
                route_type = _route_for_requested_lines(requested_lines)
            except ValueError as exc:
                flash(str(exc), "danger")
                return redirect(request.url)
            configuration_error = _route_configuration_error(route_type, row.requester_user_id)
            if configuration_error:
                flash(configuration_error, "danger")
                return redirect(request.url)
            _replace_lines(row, requested_lines)
            row.route_type = route_type
            row.approval_stage = _initial_stage_for_route(route_type, row.requester_user_id)
            _notify(row, _recipient_ids(row), f"تم تعديل طلب المواد #{row.id} ويحتاج إعادة المتابعة لدى {STAGES.get(row.approval_stage, row.approval_stage)}.")
        db.session.add(InvEmployeeRequestAction(
            request_id=row.id,
            stage=row.approval_stage,
            action="UPDATED",
            actor_user_id=current_user.id,
            note="تم تعديل تفاصيل طلب المواد." + (" وأعيد إلى بداية مسار الاعتماد." if old_signature != new_signature else ""),
        ))
        db.session.commit()
        flash("تم حفظ تعديل الطلب.", "success")
        return redirect(request.url)
    return render_template(
        "portal/inventory/employee_request_view.html",
        item=row,
        items=items,
        categories=categories,
        warehouses=warehouses,
        item_totals=item_totals,
        warehouse_balances=warehouse_balances,
        stages=STAGES,
        stage_responsibles=_stage_responsible_labels(row),
        status_label=_request_status_label(row),
        approval_stage=_effective_approval_stage(row),
        warehouse_review_stages=WAREHOUSE_REVIEW_STAGES,
        secretary_forward_stage=STAGE_ADMIN_FINANCE,
        secretary_general_stage=STAGE_SECRETARY_GENERAL,
        previous_requests=previous_requests,
        can_process=_can_process(row),
        can_edit=can_edit,
        can_cancel=row.status == "SUBMITTED" and row.requester_user_id == current_user.id,
        can_manage_catalog=_can_manage_catalog(),
        can_return=bool(_can_manage() and returnable_lines),
        returnable_lines=returnable_lines,
    )


@portal_bp.route("/inventory/employee-requests/<int:request_id>/return", methods=["GET", "POST"])
@login_required
def inventory_employee_request_return(request_id):
    """Return issued employee-request quantities to their original warehouse."""
    row = InvEmployeeRequest.query.get_or_404(request_id)
    if not _can_manage():
        abort(403)
    if row.status not in {"APPROVED", "PARTIALLY_RETURNED"}:
        flash("لا يمكن إرجاع مواد لطلب غير معتمد ومصروف.", "warning")
        return redirect(url_for("portal.inventory_employee_request_view", request_id=row.id))

    returnable_lines = _request_returnable_lines(row)
    if not returnable_lines:
        flash("لا توجد كمية متبقية قابلة للإرجاع لهذا الطلب.", "warning")
        return redirect(url_for("portal.inventory_employee_request_view", request_id=row.id))

    if request.method == "POST":
        voucher_date = (request.form.get("voucher_date") or "").strip() or date.today().isoformat()
        note = (request.form.get("note") or "").strip() or "إرجاع مواد طلب الموظف إلى مستودع الصرف الأصلي."
        selected = []
        selected_by_line = {}
        for line, already_returned, remaining in returnable_lines:
            raw_quantity = (request.form.get(f"return_qty_{line.id}") or "0").strip()
            try:
                quantity = float(raw_quantity or 0)
            except (TypeError, ValueError):
                quantity = -1
            if quantity < 0 or quantity > remaining + 1e-9:
                flash(
                    f"كمية الإرجاع للصنف {line.item.name} غير صحيحة؛ الحد المتبقي {remaining:g}.",
                    "danger",
                )
                return redirect(url_for("portal.inventory_employee_request_return", request_id=row.id))
            if quantity > 0:
                selected.append((line, quantity))
                selected_by_line[line.id] = quantity

        if not selected:
            flash("أدخل كمية إرجاع موجبة لصنف واحد على الأقل.", "danger")
            return redirect(url_for("portal.inventory_employee_request_return", request_id=row.id))

        grouped = defaultdict(list)
        for line, quantity in selected:
            grouped[line.warehouse_id].append((line, quantity))

        voucher_nos = []
        for warehouse_id, entries in grouped.items():
            issue_ids = {line.issue_voucher_id for line, _quantity in entries if line.issue_voucher_id}
            source_issue_voucher_id = issue_ids.pop() if len(issue_ids) == 1 else None
            voucher = InvReturnVoucher(
                voucher_no="",
                voucher_date=voucher_date,
                to_warehouse_id=warehouse_id,
                from_room_name=row.requester.full_name,
                source_request_id=row.id,
                source_issue_voucher_id=source_issue_voucher_id,
                note=f"{note} طلب المواد #{row.id}",
                created_by_id=current_user.id,
                created_at=datetime.utcnow(),
            )
            db.session.add(voucher)
            db.session.flush()
            voucher.voucher_no = auto_inventory_voucher_no("return", voucher_date, voucher.id)
            voucher_nos.append(voucher.voucher_no)
            for line, quantity in entries:
                db.session.add(InvReturnVoucherLine(
                    voucher_id=voucher.id,
                    item_id=line.item_id,
                    source_request_line_id=line.id,
                    qty=quantity,
                    details=f"إرجاع مقابل طلب المواد #{row.id}",
                ))

        returned_by_line = _returned_quantities([line.id for line in row.lines])
        fully_returned = True
        for line in row.lines:
            approved = float(line.approved_qty or 0)
            returned_after = float(returned_by_line.get(line.id, 0) or 0) + float(selected_by_line.get(line.id, 0) or 0)
            if approved > returned_after + 1e-9:
                fully_returned = False
                break

        row.status = "RETURNED" if fully_returned else "PARTIALLY_RETURNED"
        row.approval_stage = "DONE"
        row.decided_at = datetime.utcnow()
        db.session.add(InvEmployeeRequestAction(
            request_id=row.id,
            stage="DONE",
            action="RETURNED" if fully_returned else "PARTIALLY_RETURNED",
            actor_user_id=current_user.id,
            note=("تم إرجاع كامل الكميات. " if fully_returned else "تم إرجاع جزء من الكميات. ")
            + "أرقام سندات الإرجاع: "
            + ", ".join(voucher_nos),
        ))
        _notify(
            row,
            [row.requester_user_id],
            (
                f"تم إرجاع كامل مواد طلب المواد #{row.id} إلى المستودع الأصلي وإلغاء أثر الاعتماد."
                if fully_returned
                else f"تم إرجاع جزء من مواد طلب المواد #{row.id} إلى المستودع الأصلي."
            ),
        )
        db.session.commit()
        flash(
            "تم إرجاع كامل المواد وإلغاء اعتماد الطلب وإخفاؤه من مؤشر الطلبات خلال الشهر."
            if fully_returned
            else "تم حفظ الإرجاع الجزئي وإضافة الكمية إلى المستودع الأصلي.",
            "success",
        )
        return redirect(url_for("portal.inventory_employee_request_view", request_id=row.id))

    return render_template(
        "portal/inventory/employee_request_return.html",
        item=row,
        returnable_lines=returnable_lines,
        today=date.today().isoformat(),
    )


@portal_bp.route("/inventory/employee-requests/<int:request_id>/form.pdf")
@login_required
def inventory_employee_request_form_pdf(request_id):
    row = InvEmployeeRequest.query.get_or_404(request_id)
    if not _can_view(row):
        abort(403)
    payload = _supply_form_payload(row)
    response = send_file(
        BytesIO(build_supply_request_pdf(payload)),
        mimetype="application/pdf",
        as_attachment=request.args.get("download") == "1",
        download_name=official_form_filename(payload["requester_name"], "طلب المواد", "pdf"),
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@portal_bp.route("/inventory/employee-requests/<int:request_id>/form.docx")
@login_required
def inventory_employee_request_form_docx(request_id):
    row = InvEmployeeRequest.query.get_or_404(request_id)
    if not _can_view(row):
        abort(403)
    payload = _supply_form_payload(row)
    response = send_file(
        BytesIO(build_supply_request_docx(payload)),
        mimetype=DOCX_MIME,
        as_attachment=True,
        download_name=official_form_filename(payload["requester_name"], "طلب المواد", "docx"),
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@portal_bp.route("/inventory/employee-requests/<int:request_id>/approve", methods=["POST"])
@login_required
def inventory_employee_request_approve(request_id):
    row = InvEmployeeRequest.query.get_or_404(request_id)
    if not _can_process(row):
        abort(403)
    note = (request.form.get("note") or "").strip() or None
    current_stage = _effective_approval_stage(row)
    if row.approval_stage != current_stage:
        row.approval_stage = current_stage
    decision = (request.form.get("decision") or "approve").strip().lower()
    if decision == "reject":
        if not note:
            flash("اكتب سبب رفض الطلب.", "danger")
            return redirect(url_for("portal.inventory_employee_request_view", request_id=row.id))
        row.status = "REJECTED"
        row.decided_at = datetime.utcnow()
        db.session.add(InvEmployeeRequestAction(
            request_id=row.id,
            stage=current_stage,
            action="REJECTED",
            actor_user_id=current_user.id,
            note=note,
        ))
        _notify(
            row,
            [row.requester_user_id],
            f"تم رفض طلب المواد #{row.id} لدى {STAGES.get(current_stage, current_stage)}. السبب: {note}",
        )
        db.session.commit()
        flash("تم رفض الطلب وإبلاغ الموظف.", "success")
        return redirect(url_for("portal.inventory_employee_request_view", request_id=row.id))
    action = "APPROVED"
    if decision == "forward_secretary":
        if current_stage != STAGE_ADMIN_FINANCE:
            abort(400)
        if not _special_stage_approver_ids(STAGE_SECRETARY_GENERAL, row.requester_user_id):
            flash("لا يمكن الإحالة إلى الأمين العام قبل تعيين حسابه أو منحه صلاحية اعتماد طلبات المواد.", "danger")
            return redirect(url_for("portal.inventory_employee_request_view", request_id=row.id))
        row.approval_stage = STAGE_SECRETARY_GENERAL
        action = "FORWARDED"
        note = note or "تمت إحالة الطلب إلى الأمين العام للاعتماد أو الرفض."
    elif decision != "approve":
        abort(400)
    elif current_stage in WAREHOUSE_REVIEW_STAGES:
        next_stage = NEXT_APPROVAL_STAGE.get(current_stage)
        if next_stage and not _special_stage_approver_ids(next_stage, row.requester_user_id):
            flash(f"لا يمكن إحالة الطلب قبل تعيين {STAGES.get(next_stage, next_stage)}.", "danger")
            return redirect(url_for("portal.inventory_employee_request_view", request_id=row.id))
        try:
            _prepare_issue_from_warehouse_review(row)
        except ValueError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("portal.inventory_employee_request_view", request_id=row.id))
        if next_stage:
            row.approval_stage = next_stage
        else:
            try:
                _create_issue_vouchers(row, validated=True)
            except ValueError as exc:
                flash(str(exc), "danger")
                return redirect(url_for("portal.inventory_employee_request_view", request_id=row.id))
            row.status = "APPROVED"
            row.approval_stage = "DONE"
            row.decided_at = datetime.utcnow()
    elif current_stage == STAGE_TECH_DIRECTOR:
        next_stage = NEXT_APPROVAL_STAGE[current_stage]
        if not _special_stage_approver_ids(next_stage, row.requester_user_id):
            flash(f"لا يمكن إحالة الطلب قبل تعيين {STAGES.get(next_stage, next_stage)}.", "danger")
            return redirect(url_for("portal.inventory_employee_request_view", request_id=row.id))
        row.approval_stage = next_stage
    elif current_stage in {STAGE_ADMIN_FINANCE, STAGE_SECRETARY_GENERAL}:
        prepared_error = _prepared_issue_error(row)
        if prepared_error:
            flash(prepared_error, "danger")
            return redirect(url_for("portal.inventory_employee_request_view", request_id=row.id))
        try:
            _create_issue_vouchers(row, validated=True)
        except ValueError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("portal.inventory_employee_request_view", request_id=row.id))
        row.status = "APPROVED"
        row.approval_stage = "DONE"
        row.decided_at = datetime.utcnow()
    else:
        abort(400)
    db.session.add(InvEmployeeRequestAction(
        request_id=row.id,
        stage=current_stage,
        action=action,
        actor_user_id=current_user.id,
        note=note,
    ))
    if row.status == "SUBMITTED":
        _notify(row, _recipient_ids(row), f"طلب المواد #{row.id} بانتظار متابعتك لدى {STAGES.get(row.approval_stage, row.approval_stage)}.")
    else:
        _notify(row, [row.requester_user_id], f"تم اعتماد طلب المواد #{row.id} نهائياً وصرف المواد من المستودع.")
    db.session.commit()
    flash("تمت متابعة طلب المواد." if row.status == "SUBMITTED" else "تم الاعتماد النهائي وإنشاء سند الصرف وخصم الكميات.", "success")
    return redirect(url_for("portal.inventory_employee_request_view", request_id=row.id))


@portal_bp.route("/inventory/employee-requests/<int:request_id>/cancel", methods=["POST"])
@login_required
def inventory_employee_request_cancel(request_id):
    row = InvEmployeeRequest.query.get_or_404(request_id)
    if row.requester_user_id != current_user.id:
        abort(403)
    if row.status != "SUBMITTED":
        flash("لا يمكن إلغاء الطلب بعد رفضه أو صرف مواده.", "warning")
        return redirect(url_for("portal.inventory_employee_request_view", request_id=row.id))
    note = (request.form.get("note") or "").strip() or "ألغى الموظف طلب المواد."
    recipients = _recipient_ids(row)
    current_stage = row.approval_stage
    row.status = "CANCELLED"
    row.decided_at = datetime.utcnow()
    db.session.add(InvEmployeeRequestAction(
        request_id=row.id,
        stage=current_stage,
        action="CANCELLED",
        actor_user_id=current_user.id,
        note=note,
    ))
    _notify(row, recipients, f"ألغى الموظف طلب المواد #{row.id}. لم يعد يحتاج إلى إجراء.")
    db.session.commit()
    flash("تم إلغاء طلب المواد.", "success")
    return redirect(url_for("portal.inventory_employee_request_view", request_id=row.id))


@portal_bp.route("/inventory/employee-requests/report")
@login_required
def inventory_employee_requests_report():
    if not _can_manage() and not current_user.has_perm("PORTAL_REPORTS_READ"):
        abort(403)
    selected_month = (request.args.get("month") or "").strip()
    selected_user_id = (request.args.get("user_id") or "").strip()
    selected_item_id = (request.args.get("item_id") or "").strip()
    query = (
        InvEmployeeRequestLine.query
        .join(InvEmployeeRequest, InvEmployeeRequest.id == InvEmployeeRequestLine.request_id)
        .filter(
            InvEmployeeRequest.status.in_(("APPROVED", "PARTIALLY_RETURNED")),
            InvEmployeeRequestLine.issue_voucher_id.isnot(None),
        )
    )
    if selected_month:
        query = query.join(InvIssueVoucher, InvIssueVoucher.id == InvEmployeeRequestLine.issue_voucher_id).filter(
            InvIssueVoucher.voucher_date.like(f"{selected_month}%")
        )
    if selected_user_id.isdigit():
        query = query.filter(InvEmployeeRequest.requester_user_id == int(selected_user_id))
    if selected_item_id.isdigit():
        query = query.filter(InvEmployeeRequestLine.item_id == int(selected_item_id))
    rows = query.order_by(InvEmployeeRequest.decided_at.desc(), InvEmployeeRequestLine.id.desc()).all()
    returned_by_line = _returned_quantities([line.id for line in rows])
    item_summary = defaultdict(float)
    employee_summary = defaultdict(float)
    for line in rows:
        line.returned_qty = min(
            float(line.approved_qty or 0),
            float(returned_by_line.get(line.id, 0) or 0),
        )
        line.consumed_qty = max(0.0, float(line.approved_qty or 0) - line.returned_qty)
        item_summary[line.item.label] += line.consumed_qty
        employee_summary[line.request.requester.full_name] += line.consumed_qty
    return render_template(
        "portal/inventory/employee_consumption_report.html",
        rows=rows,
        item_summary=sorted(item_summary.items(), key=lambda pair: pair[0]),
        employee_summary=sorted(employee_summary.items(), key=lambda pair: pair[0]),
        users=User.query.order_by(User.name.asc()).all(),
        items=InvItem.query.order_by(InvItem.name.asc()).all(),
        selected_month=selected_month,
        selected_user_id=int(selected_user_id) if selected_user_id.isdigit() else None,
        selected_item_id=int(selected_item_id) if selected_item_id.isdigit() else None,
    )
