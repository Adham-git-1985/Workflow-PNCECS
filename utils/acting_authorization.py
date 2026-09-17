"""Authorization and execution context for acting permissions.

The application historically had a broad ``Delegation`` table that is still
used by a few legacy workflow paths.  This module implements the newer,
explicit model from the technical proposal without replacing that legacy
contract:

* ``ActingPermission`` grants only selected operational actions.
* ``FormalDelegation`` grants an administrative/legal authority backed by a
  reference and an explicit delegation type.
* ``current_user`` is always the real logged-in user.  Selecting a context
  stores IDs in the session and exposes the principal through ``g`` only as
  metadata; it never changes Flask-Login's identity.

All action paths that adopt this layer can use ``authorize_action`` before
mutating data and ``record_execution_audit`` after the mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from functools import wraps
import json
import re
import unicodedata
from typing import Any, Iterable

from flask import abort, g, has_request_context, request, session
from flask_login import current_user
from sqlalchemy import or_

from extensions import db
from models import (
    ActingPermission,
    AuditLog,
    FormalDelegation,
    Notification,
    User,
)


SELF = "SELF"
ACTING = "ACTING"
DELEGATED = "DELEGATED"
ACTING_AND_DELEGATED = "ACTING_AND_DELEGATED"

ACTIVE = "ACTIVE"
REVOKED = "REVOKED"
EXPIRED = "EXPIRED"

ACTING_PERMISSION_CREATE = "ACTING_PERMISSION_CREATE"
ACTING_PERMISSION_MANAGE = "ACTING_PERMISSION_MANAGE"
FORMAL_DELEGATION_CREATE = "CAN_CREATE_DELEGATION"
FORMAL_DELEGATION_MANAGE = "FORMAL_DELEGATION_MANAGE"

ACTING_PERMISSION_SESSION_KEY = "acting_permission_id"
ACTING_FOR_SESSION_KEY = "acting_for_user_id"
FORMAL_DELEGATION_SESSION_KEY = "formal_delegation_id"


class AuthorizationError(PermissionError):
    """Raised when an acting/formal execution context is not authorized."""


@dataclass(frozen=True)
class ExecutionAuthorization:
    """The immutable authorization decision used for audit/notifications."""

    actual_user_id: int
    acting_for_user_id: int
    action: str
    module_id: str | None = None
    acting_permission: ActingPermission | None = None
    formal_delegation: FormalDelegation | None = None
    execution_context: str = SELF

    @property
    def is_acting(self) -> bool:
        return self.execution_context in {ACTING, ACTING_AND_DELEGATED}

    @property
    def is_formal(self) -> bool:
        return self.execution_context in {DELEGATED, ACTING_AND_DELEGATED}


ACTION_FIELDS = {
    "VIEW": "can_view",
    "READ": "can_view",
    "DISPLAY": "can_view",
    "CREATE": "can_create",
    "NEW": "can_create",
    "EDIT": "can_edit",
    "UPDATE": "can_edit",
    "MODIFY": "can_edit",
    "FORWARD": "can_forward",
    "TRANSFER": "can_forward",
    "FOLLOW": "can_follow_up",
    "FOLLOW_UP": "can_follow_up",
    "FOLLOWUP": "can_follow_up",
    "COMPLETE": "can_edit",
    "CONTINUE": "can_edit",
    "EXECUTE_TASK": "can_follow_up",
    "TASK": "can_follow_up",
    "ATTACH": "can_edit",
    "ADD_ATTACHMENT": "can_edit",
    "REFER": "can_forward",
    "RETURN": "can_forward",
    "REJECT": "can_reject",
    "CANCEL": "can_cancel",
    "CLOSE": "can_close",
    "REOPEN": "can_reopen",
    # Kept for compatibility with older data/forms.  It is deliberately not
    # enough to authorize an action in FORMAL_REQUIRED_ACTIONS below.
    "APPROVE": "can_approve",
}

FORMAL_REQUIRED_ACTIONS = frozenset({
    "APPROVE",
    "CONSENT",
    "SIGN",
    "SIGNATURE",
    "FINANCIAL_APPROVE",
    "APPROVE_FINANCIAL",
    "ISSUE_DECISION",
    "DECIDE",
})

SENSITIVE_ACTIONS = frozenset({
    "APPROVE",
    "CONSENT",
    "SIGN",
    "SIGNATURE",
    "FINANCIAL_APPROVE",
    "APPROVE_FINANCIAL",
    "ISSUE_DECISION",
    "DECIDE",
    "REJECT",
    "CANCEL",
    "CLOSE",
    "REOPEN",
})

FORMAL_TYPE_ACTIONS = {
    "APPROVAL": {"APPROVE", "DECIDE"},
    "APPROVE": {"APPROVE", "DECIDE"},
    "SIGNATURE": {"SIGN", "SIGNATURE"},
    "SIGN": {"SIGN", "SIGNATURE"},
    "CONSENT": {"CONSENT", "APPROVE"},
    "APPROVAL_CONSENT": {"APPROVE", "CONSENT", "DECIDE"},
    "FINANCIAL_APPROVAL": {"FINANCIAL_APPROVE", "APPROVE_FINANCIAL", "APPROVE"},
    "FINANCIAL_APPROVE": {"FINANCIAL_APPROVE", "APPROVE_FINANCIAL", "APPROVE"},
    "DECISION": {"ISSUE_DECISION", "DECIDE"},
    "ISSUE_DECISION": {"ISSUE_DECISION", "DECIDE"},
    "ADMINISTRATIVE_AUTHORITY": {"*"},
    "ADMIN_AUTHORITY": {"*"},
    "ALL": {"*"},
}

ACTION_LABELS_AR = {
    "VIEW": "الاطلاع",
    "READ": "الاطلاع",
    "CREATE": "الإنشاء",
    "EDIT": "التعديل",
    "UPDATE": "التعديل",
    "FORWARD": "التحويل",
    "FOLLOW": "المتابعة",
    "FOLLOW_UP": "المتابعة",
    "COMPLETE": "استكمال النواقص",
    "CONTINUE": "الاستكمال",
    "REOPEN": "إعادة الفتح",
    "ATTACH": "إضافة مرفق",
    "ADD_ATTACHMENT": "إضافة مرفق",
    "REFER": "الإحالة",
    "RETURN": "إعادة المعاملة",
    "REJECT": "الرفض",
    "CANCEL": "الإلغاء",
    "CLOSE": "الإغلاق",
    "APPROVE": "الاعتماد",
    "CONSENT": "الموافقة",
    "EXECUTE_TASK": "تنفيذ المهمة",
    "TASK": "تنفيذ المهمة",
    "SIGN": "التوقيع",
    "FINANCIAL_APPROVE": "الاعتماد المالي",
    "ISSUE_DECISION": "إصدار القرار",
}


def _norm(value: Any) -> str:
    text = str(value or "").strip()
    try:
        text = unicodedata.normalize("NFKC", text)
    except Exception:
        pass
    return text.upper().replace("-", "_").replace(" ", "_")


def normalize_action(value: Any) -> str:
    """Normalize English/Arabic action labels to a stable action code."""
    raw = str(value or "").strip()
    normalized = _norm(raw)
    arabic = {
        "عرض": "VIEW",
        "اطلاع": "VIEW",
        "إنشاء": "CREATE",
        "انشاء": "CREATE",
        "تعديل": "EDIT",
        "تحويل": "FORWARD",
        "متابعة": "FOLLOW_UP",
        "استكمال": "COMPLETE",
        "إعادة فتح": "REOPEN",
        "اعادة فتح": "REOPEN",
        "إضافة مرفق": "ADD_ATTACHMENT",
        "اضافة مرفق": "ADD_ATTACHMENT",
        "إحالة": "REFER",
        "احالة": "REFER",
        "إعادة معاملة": "RETURN",
        "اعادة معاملة": "RETURN",
        "رفض": "REJECT",
        "إلغاء": "CANCEL",
        "الغاء": "CANCEL",
        "إغلاق": "CLOSE",
        "اغلاق": "CLOSE",
        "اعتماد": "APPROVE",
        "توقيع": "SIGN",
        "اعتماد_مالي": "FINANCIAL_APPROVE",
        "اعتماد مالي": "FINANCIAL_APPROVE",
        "إصدار قرار": "ISSUE_DECISION",
        "اصدار قرار": "ISSUE_DECISION",
    }
    return arabic.get(raw, normalized)


def _id(value: Any) -> int | None:
    value = getattr(value, "id", value)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _now(value: datetime | None = None) -> datetime:
    return value or datetime.utcnow()


def _current_user_id() -> int | None:
    try:
        if getattr(current_user, "is_authenticated", False):
            return _id(current_user)
    except Exception:
        pass
    return None


def _status_is_active(value: Any) -> bool:
    return _norm(value or ACTIVE) == ACTIVE


def expire_due_records(now: datetime | None = None, *, auto_commit: bool = False) -> dict[str, int]:
    """Mark elapsed active grants as ``EXPIRED``.

    Authorization also checks the time window, so a missed scheduler tick can
    never leave an expired grant usable.  This function is safe to run on each
    request or from a periodic job.
    """
    now = _now(now)
    changed = {"acting_permissions": 0, "formal_delegations": 0}
    try:
        acting_rows = ActingPermission.query.filter(
            ActingPermission.status == ACTIVE,
            ActingPermission.end_at < now,
        ).all()
        for row in acting_rows:
            row.status = EXPIRED
            changed["acting_permissions"] += 1
            db.session.add(Notification(
                user_id=int(row.acting_user_id),
                message=(
                    f"انتهت صلاحيتك للعمل بالنيابة عن {_actor_label(row.principal_user_id)} "
                    f"(الصلاحية رقم {row.id})."
                )[:255],
                type="DELEGATION_EXPIRED",
                source="workflow",
                link_url="/delegation/permissions",
                email_delivery_mode="GENERAL",
                is_visible=True,
                is_read=False,
            ))

        formal_rows = FormalDelegation.query.filter(
            FormalDelegation.status == ACTIVE,
            FormalDelegation.end_at < now,
        ).all()
        for row in formal_rows:
            row.status = EXPIRED
            changed["formal_delegations"] += 1
            reference = row.decision_number or row.legal_reference or f"#{row.id}"
            db.session.add(Notification(
                user_id=int(row.delegate_user_id),
                message=(
                    f"انتهى التفويض الرسمي {reference} الصادر عن "
                    f"{_actor_label(row.delegator_user_id)} (السجل رقم {row.id})."
                )[:255],
                type="FORMAL_DELEGATION_EXPIRED",
                source="workflow",
                link_url="/delegation/permissions",
                email_delivery_mode="GENERAL",
                is_visible=True,
                is_read=False,
            ))

        if auto_commit and any(changed.values()):
            db.session.commit()
    except Exception:
        db.session.rollback()
    return changed


def get_active_acting_permissions(user: Any = None, now: datetime | None = None) -> list[ActingPermission]:
    """Return currently effective acting permissions for an actor."""
    user_id = _id(user) if user is not None else _current_user_id()
    if not user_id:
        return []
    at = _now(now)
    try:
        rows = (
            ActingPermission.query
            .filter(
                ActingPermission.acting_user_id == user_id,
                ActingPermission.status == ACTIVE,
                ActingPermission.start_at <= at,
                ActingPermission.end_at >= at,
            )
            .order_by(ActingPermission.end_at.asc(), ActingPermission.id.asc())
            .all()
        )
        return [row for row in rows if row.is_effective_at(at)]
    except Exception:
        return []


def get_active_formal_delegations(user: Any = None, now: datetime | None = None) -> list[FormalDelegation]:
    """Return currently effective formal delegations for a delegatee."""
    user_id = _id(user) if user is not None else _current_user_id()
    if not user_id:
        return []
    at = _now(now)
    try:
        rows = (
            FormalDelegation.query
            .filter(
                FormalDelegation.delegate_user_id == user_id,
                FormalDelegation.status == ACTIVE,
                FormalDelegation.start_at <= at,
                FormalDelegation.end_at >= at,
            )
            .order_by(FormalDelegation.end_at.asc(), FormalDelegation.id.asc())
            .all()
        )
        return [row for row in rows if row.is_effective_at(at)]
    except Exception:
        return []


def _session_int(key: str) -> int | None:
    if not has_request_context():
        return None
    try:
        return _id(session.get(key))
    except Exception:
        return None


def load_execution_context(force: bool = False) -> dict[str, Any]:
    """Load the explicit acting/formal selection for the current request."""
    if not has_request_context():
        return {
            "actual_user": None,
            "acting_for_user": None,
            "acting_permission": None,
            "formal_delegation": None,
            "execution_context": SELF,
        }
    if getattr(g, "execution_context_loaded", False) and not force:
        return get_execution_context()

    actual_id = _current_user_id()
    actual_user = current_user if actual_id else None
    acting_permission = None
    formal_delegation = None

    if actual_id:
        permission_id = _session_int(ACTING_PERMISSION_SESSION_KEY)
        if permission_id:
            acting_permission = next(
                (row for row in get_active_acting_permissions(actual_id) if _id(row) == permission_id),
                None,
            )
            if not acting_permission:
                session.pop(ACTING_PERMISSION_SESSION_KEY, None)
                session.pop(ACTING_FOR_SESSION_KEY, None)

        formal_id = _session_int(FORMAL_DELEGATION_SESSION_KEY)
        if formal_id:
            formal_delegation = next(
                (row for row in get_active_formal_delegations(actual_id) if _id(row) == formal_id),
                None,
            )
            if not formal_delegation:
                session.pop(FORMAL_DELEGATION_SESSION_KEY, None)

        # A combined context is meaningful only when both grants refer to the
        # same principal.  Never silently combine unrelated authorities.
        if (
            acting_permission
            and formal_delegation
            and int(acting_permission.principal_user_id) != int(formal_delegation.delegator_user_id)
        ):
            formal_delegation = None
            session.pop(FORMAL_DELEGATION_SESSION_KEY, None)

    acting_for_user = actual_user
    if acting_permission:
        acting_for_user = acting_permission.principal_user
    elif formal_delegation:
        acting_for_user = formal_delegation.delegator_user

    if acting_permission and formal_delegation:
        execution_context = ACTING_AND_DELEGATED
    elif acting_permission:
        execution_context = ACTING
    elif formal_delegation:
        execution_context = DELEGATED
    else:
        execution_context = SELF

    g.execution_context_loaded = True
    g.actual_user = actual_user
    g.actual_user_id = actual_id
    g.acting_permission = acting_permission
    g.acting_permission_id = _id(acting_permission)
    g.formal_delegation = formal_delegation
    g.formal_delegation_id = _id(formal_delegation)
    g.acting_for_user = acting_for_user
    g.acting_for_user_id = _id(acting_for_user) or actual_id
    g.execution_context = execution_context
    g.active_acting_permissions = get_active_acting_permissions(actual_id) if actual_id else []
    g.active_formal_delegations = get_active_formal_delegations(actual_id) if actual_id else []
    return get_execution_context()


def get_execution_context() -> dict[str, Any]:
    """Return the request-local explicit execution context."""
    if not has_request_context():
        return {
            "actual_user": None,
            "acting_for_user": None,
            "acting_permission": None,
            "formal_delegation": None,
            "execution_context": SELF,
        }
    if not getattr(g, "execution_context_loaded", False):
        return load_execution_context()
    return {
        "actual_user": getattr(g, "actual_user", None),
        "actual_user_id": getattr(g, "actual_user_id", None),
        "acting_for_user": getattr(g, "acting_for_user", None),
        "acting_for_user_id": getattr(g, "acting_for_user_id", None),
        "acting_permission": getattr(g, "acting_permission", None),
        "acting_permission_id": getattr(g, "acting_permission_id", None),
        "formal_delegation": getattr(g, "formal_delegation", None),
        "formal_delegation_id": getattr(g, "formal_delegation_id", None),
        "execution_context": getattr(g, "execution_context", SELF),
    }


def get_acting_for_user():
    return load_execution_context().get("acting_for_user")


def get_active_acting_permission():
    return load_execution_context().get("acting_permission")


def get_active_formal_delegation():
    return load_execution_context().get("formal_delegation")


def select_acting_permission(permission_id: Any) -> ActingPermission:
    """Select an active acting permission without changing ``current_user``."""
    actual_id = _current_user_id()
    if not actual_id:
        raise AuthorizationError("يجب تسجيل الدخول أولًا")
    permission = next(
        (row for row in get_active_acting_permissions(actual_id) if _id(row) == _id(permission_id)),
        None,
    )
    if not permission:
        raise AuthorizationError("الصلاحية بالنيابة غير موجودة أو انتهت")
    session[ACTING_PERMISSION_SESSION_KEY] = int(permission.id)
    session[ACTING_FOR_SESSION_KEY] = int(permission.principal_user_id)
    load_execution_context(force=True)
    return permission


def select_formal_delegation(delegation_id: Any) -> FormalDelegation:
    """Select an active formal delegation without changing ``current_user``."""
    actual_id = _current_user_id()
    if not actual_id:
        raise AuthorizationError("يجب تسجيل الدخول أولًا")
    delegation = next(
        (row for row in get_active_formal_delegations(actual_id) if _id(row) == _id(delegation_id)),
        None,
    )
    if not delegation:
        raise AuthorizationError("التفويض الرسمي غير موجود أو انتهت مدته")

    selected_permission = get_active_acting_permission()
    if selected_permission and int(selected_permission.principal_user_id) != int(delegation.delegator_user_id):
        # Keep the contexts independent; a user can switch to this formal
        # delegation without carrying an unrelated acting grant.
        session.pop(ACTING_PERMISSION_SESSION_KEY, None)
        session.pop(ACTING_FOR_SESSION_KEY, None)
    session[FORMAL_DELEGATION_SESSION_KEY] = int(delegation.id)
    load_execution_context(force=True)
    return delegation


def clear_execution_context() -> None:
    """Return the session to the user's personal working mode."""
    if has_request_context():
        for key in (
            ACTING_PERMISSION_SESSION_KEY,
            ACTING_FOR_SESSION_KEY,
            FORMAL_DELEGATION_SESSION_KEY,
        ):
            session.pop(key, None)
        for key in (
            "execution_context_loaded",
            "actual_user",
            "actual_user_id",
            "acting_permission",
            "acting_permission_id",
            "formal_delegation",
            "formal_delegation_id",
            "acting_for_user",
            "acting_for_user_id",
            "execution_context",
        ):
            g.pop(key, None)
        load_execution_context(force=True)


def _as_scope_values(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (set, tuple, list)):
        values = value
    else:
        text = str(value).strip()
        if not text:
            return set()
        if text.startswith("["):
            try:
                values = json.loads(text)
            except Exception:
                values = re.split(r"[,|]", text)
        else:
            values = re.split(r"[,|]", text)
    return {_norm(item) for item in values if str(item or "").strip()}


def _same_value(left: Any, right: Any) -> bool:
    left_values = _as_scope_values(left)
    right_values = _as_scope_values(right)
    return bool(left_values and right_values and bool(left_values.intersection(right_values)))


def _scope_matches(
    grant: Any,
    *,
    module_id: Any = None,
    scope_type: Any = None,
    scope_id: Any = None,
    transaction_type: Any = None,
    request_type: Any = None,
    scope: dict[str, Any] | None = None,
    request_obj: Any = None,
) -> bool:
    """Match module/operation/transaction scope without assuming one schema."""
    scope = dict(scope or {})
    if request_obj is not None:
        scope.setdefault("request_id", getattr(request_obj, "id", None))
        scope.setdefault("request_type", getattr(request_obj, "request_type_id", None))
        scope.setdefault("transaction_type", getattr(request_obj, "transaction_type", None))
        for key in (
            "unit_id",
            "department_id",
            "directorate_id",
            "project_id",
            "organization_id",
            "org_node_id",
            "procedure",
            "action",
        ):
            if key not in scope and hasattr(request_obj, key):
                scope[key] = getattr(request_obj, key)

    requested_module = module_id or scope.get("module_id") or scope.get("module") or scope.get("module_name")
    grant_module = getattr(grant, "module_id", None) or getattr(grant, "module_name", None)
    if grant_module and (not requested_module or not _same_value(grant_module, requested_module)):
        return False

    grant_transaction = getattr(grant, "transaction_type", None)
    requested_transaction = transaction_type or scope.get("transaction_type")
    if grant_transaction and (not requested_transaction or not _same_value(grant_transaction, requested_transaction)):
        return False

    grant_request_type = getattr(grant, "request_type", None)
    requested_request_type = request_type or scope.get("request_type")
    if grant_request_type and (not requested_request_type or not _same_value(grant_request_type, requested_request_type)):
        return False

    grant_scope_type = _norm(getattr(grant, "scope_type", None) or "ALL")
    grant_scope_id = getattr(grant, "scope_id", None)
    if grant_scope_type in {"", "ALL", "SYSTEM", "GLOBAL"} or not grant_scope_id:
        return True

    requested_scope_type = _norm(scope_type or scope.get("scope_type") or grant_scope_type)
    if grant_scope_type in {"MODULE", "SUBSYSTEM"}:
        requested_value = requested_module or scope_id or scope.get("scope_id")
    elif grant_scope_type in {"TRANSACTION", "TRANSACTION_TYPE", "REQUEST_TYPE", "PROJECT", "ACTION", "PROCEDURE", "REQUEST"}:
        keys = {
            "TRANSACTION": "transaction_type",
            "TRANSACTION_TYPE": "transaction_type",
            "REQUEST_TYPE": "request_type",
            "PROJECT": "project_id",
            "ACTION": "action",
            "PROCEDURE": "procedure",
            "REQUEST": "request_id",
        }
        requested_value = scope.get(keys.get(grant_scope_type, "scope_id"), None)
        if requested_value is None and requested_scope_type == grant_scope_type:
            requested_value = scope_id or scope.get("scope_id")
    else:
        key = grant_scope_type.lower()
        requested_value = scope.get(key)
        if requested_value is None:
            requested_value = scope.get(f"{key}_id")
        if requested_value is None and requested_scope_type == grant_scope_type:
            requested_value = scope_id or scope.get("scope_id")

    return _same_value(grant_scope_id, requested_value)


def _formal_type_allows(delegation_type: Any, action: str) -> bool:
    # Reading is a prerequisite for an authorized action, but is not itself a
    # legal/administrative authority.  The workflow still applies its normal
    # visibility/confidentiality rules to the principal below.
    if action in {"VIEW", "READ", "DISPLAY"}:
        return True
    type_code = normalize_action(delegation_type)
    type_code = {
        "اعتماد": "APPROVAL",
        "توقيع": "SIGNATURE",
        "موافقة": "CONSENT",
        "اعتماد_مالي": "FINANCIAL_APPROVAL",
        "إصدار_قرار": "ISSUE_DECISION",
        "اصدار_قرار": "ISSUE_DECISION",
    }.get(str(delegation_type or "").strip(), type_code)
    allowed = FORMAL_TYPE_ACTIONS.get(type_code)
    if allowed is None:
        # Stable custom type codes can explicitly name their action.
        return action == type_code or action in type_code.split("_")
    return "*" in allowed or action in allowed


def _context_grants(
    *,
    acting_permission_id: Any = None,
    formal_delegation_id: Any = None,
    acting_permission: ActingPermission | None = None,
    formal_delegation: FormalDelegation | None = None,
):
    if acting_permission is None and acting_permission_id:
        acting_permission = db.session.get(ActingPermission, _id(acting_permission_id))
    if formal_delegation is None and formal_delegation_id:
        formal_delegation = db.session.get(FormalDelegation, _id(formal_delegation_id))
    if acting_permission is None and formal_delegation is None and has_request_context():
        context = load_execution_context()
        acting_permission = context.get("acting_permission")
        formal_delegation = context.get("formal_delegation")
    return acting_permission, formal_delegation


def is_self_approval_conflict(actual_user_id: Any, target: Any, action: Any = "APPROVE") -> bool:
    """Return true when the real actor created/submitted the target."""
    if normalize_action(action) not in FORMAL_REQUIRED_ACTIONS:
        return False
    actor_id = _id(actual_user_id)
    target_id = _id(target)
    if not actor_id or target is None:
        return False

    for attribute in (
        "created_by_id",
        "created_by_user_id",
        "submitted_by_id",
        "requester_id",
        "entered_by_user_id",
    ):
        if _id(getattr(target, attribute, None)) == actor_id:
            return True

    # Some legacy records do not carry a creator column.  Their CREATE audit
    # is still authoritative and uses the actual actor where available.
    try:
        request_id = _id(getattr(target, "request_id", None))
        query = AuditLog.query.filter(
            or_(AuditLog.action.ilike("%CREATE%"), AuditLog.action.ilike("%SUBMIT%")),
            AuditLog.user_id == actor_id,
        )
        if request_id:
            query = query.filter(AuditLog.request_id == request_id)
        elif target_id:
            query = query.filter(AuditLog.target_id == target_id)
        return query.first() is not None
    except Exception:
        return False


def assert_not_self_approval(actual_user_id: Any, target: Any, action: Any = "APPROVE") -> None:
    if is_self_approval_conflict(actual_user_id, target, action):
        raise AuthorizationError("لا يجوز للمستخدم الفعلي اعتماد إجراء أنشأه بنفسه")


def authorize_action(
    action: Any,
    *,
    module_id: Any = None,
    module_name: Any = None,
    scope_type: Any = None,
    scope_id: Any = None,
    transaction_type: Any = None,
    request_type: Any = None,
    scope: dict[str, Any] | None = None,
    request_obj: Any = None,
    actual_user: Any = None,
    acting_for_user_id: Any = None,
    acting_permission_id: Any = None,
    formal_delegation_id: Any = None,
    acting_permission: ActingPermission | None = None,
    formal_delegation: FormalDelegation | None = None,
    require_formal: bool | None = None,
    apply_context: bool = True,
) -> ExecutionAuthorization:
    """Authorize one operation in SELF, ACTING or DELEGATED context.

    This function does not replace a module's ordinary RBAC check for a
    personal action.  For a selected acting/formal context it is the required
    scoped check and fails closed when the matching grant is absent.
    """
    action_code = normalize_action(action)
    actual_id = _id(actual_user) if actual_user is not None else _current_user_id()
    if not actual_id:
        raise AuthorizationError("يجب تسجيل الدخول أولًا")

    acting_permission, formal_delegation = _context_grants(
        acting_permission_id=acting_permission_id,
        formal_delegation_id=formal_delegation_id,
        acting_permission=acting_permission,
        formal_delegation=formal_delegation,
    )

    # Explicit principal selection is useful to service callers that do not
    # have a browser session, but it never authorizes anything by itself.
    if acting_for_user_id and not acting_permission and not formal_delegation:
        raise AuthorizationError("لا توجد صلاحية أو تفويض مرتبط بالسياق المحدد")

    if acting_permission:
        if (
            not acting_permission.is_effective_at()
            or int(acting_permission.acting_user_id) != int(actual_id)
        ):
            raise AuthorizationError("الصلاحية بالنيابة غير فعالة أو لا تخص المستخدم الحالي")
        principal_id = int(acting_permission.principal_user_id)
    else:
        principal_id = _id(acting_for_user_id) or actual_id

    if formal_delegation:
        if (
            not formal_delegation.is_effective_at()
            or int(formal_delegation.delegate_user_id) != int(actual_id)
        ):
            raise AuthorizationError("التفويض الرسمي غير فعّال أو لا يخص المستخدم الحالي")
        if not (formal_delegation.legal_reference or formal_delegation.decision_number):
            raise AuthorizationError("التفويض الرسمي بلا مرجعية أو رقم قرار")
        if acting_permission and int(formal_delegation.delegator_user_id) != principal_id:
            raise AuthorizationError("لا يمكن دمج صلاحية وتفويض صادرين عن شخصين مختلفين")
        principal_id = int(formal_delegation.delegator_user_id)

    # Personal mode: the real account remains the subject.  The caller's
    # normal module RBAC still decides whether this personal action is legal.
    if not acting_permission and not formal_delegation and principal_id == int(actual_id):
        decision = ExecutionAuthorization(
            actual_user_id=int(actual_id),
            acting_for_user_id=int(actual_id),
            action=action_code,
            module_id=str(module_id or module_name or "") or None,
            execution_context=SELF,
        )
        if apply_context and has_request_context():
            apply_execution_context(decision)
        return decision

    formal_required = action_code in FORMAL_REQUIRED_ACTIONS if require_formal is None else bool(require_formal)

    # For a combined context, the grant that actually authorizes the action
    # controls its scope: operational actions use the acting permission;
    # approval/signature/decision actions use the formal delegation.  This
    # prevents an unrelated grant from accidentally narrowing or broadening
    # the other authority.
    scope_grant = formal_delegation if formal_required and formal_delegation else acting_permission or formal_delegation
    if not _scope_matches(
        scope_grant,
        module_id=module_id or module_name,
        scope_type=scope_type,
        scope_id=scope_id,
        transaction_type=transaction_type,
        request_type=request_type,
        scope=scope,
        request_obj=request_obj,
    ):
        raise AuthorizationError("الإجراء خارج نطاق الصلاحية أو التفويض")

    # In a combined context, the formal record authorizes formal actions while
    # the independent acting grant authorizes operational actions.  Do not
    # make an approval-only formal type veto an otherwise valid operational
    # acting grant.
    if formal_delegation and (formal_required or not acting_permission):
        if not _formal_type_allows(formal_delegation.delegation_type, action_code):
            raise AuthorizationError("نوع التفويض الرسمي لا يشمل هذا الإجراء")
    elif formal_required:
        # Explicitly prevent can_approve in an acting grant from becoming a
        # hidden approval delegation.
        raise AuthorizationError("هذا الإجراء يتطلب تفويضًا رسميًا مستقلًا")

    if acting_permission and not (formal_delegation and formal_required):
        field_name = ACTION_FIELDS.get(action_code)
        if not field_name or not bool(getattr(acting_permission, field_name, False)):
            raise AuthorizationError("الإجراء غير ممنوح في الصلاحية بالنيابة")
        if action_code in FORMAL_REQUIRED_ACTIONS:
            raise AuthorizationError("لا تتحول الصلاحية التشغيلية إلى تفويض اعتماد")

    assert_not_self_approval(actual_id, request_obj, action_code)

    context_name = (
        ACTING_AND_DELEGATED
        if acting_permission and formal_delegation
        else DELEGATED
        if formal_delegation
        else ACTING
    )
    decision = ExecutionAuthorization(
        actual_user_id=int(actual_id),
        acting_for_user_id=int(principal_id),
        action=action_code,
        module_id=str(module_id or module_name or "") or None,
        acting_permission=acting_permission,
        formal_delegation=formal_delegation,
        execution_context=context_name,
    )
    if apply_context and has_request_context():
        apply_execution_context(decision)
    return decision


def can_execute_action(action: Any, **kwargs) -> bool:
    try:
        authorize_action(action, **kwargs)
        return True
    except (AuthorizationError, PermissionError, ValueError, TypeError):
        return False


def apply_execution_context(decision: ExecutionAuthorization) -> None:
    """Publish a decision so every audit row in the request is enriched."""
    if not has_request_context():
        return
    g.execution_context_loaded = True
    g.actual_user_id = int(decision.actual_user_id)
    g.acting_for_user_id = int(decision.acting_for_user_id)
    g.acting_permission = decision.acting_permission
    g.acting_permission_id = _id(decision.acting_permission)
    g.formal_delegation = decision.formal_delegation
    g.formal_delegation_id = _id(decision.formal_delegation)
    g.execution_context = decision.execution_context
    if decision.acting_permission:
        g.acting_for_user = decision.acting_permission.principal_user
    elif decision.formal_delegation:
        g.acting_for_user = decision.formal_delegation.delegator_user
    else:
        g.acting_for_user = current_user


def _request_ip() -> str | None:
    if not has_request_context():
        return None
    value = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
             or request.headers.get("X-Real-IP", "").strip()
             or (request.remote_addr or ""))
    return value[:64] or None


def _actor_label(user_id: Any) -> str:
    try:
        user = db.session.get(User, _id(user_id))
        return (user.full_name or user.username or user.email or f"المستخدم #{user.id}") if user else f"المستخدم #{user_id}"
    except Exception:
        return f"المستخدم #{user_id}"


def execution_audit_fields(decision: ExecutionAuthorization | None = None) -> dict[str, Any]:
    """Return canonical + legacy audit fields for the active execution."""
    if decision is None and has_request_context():
        context = load_execution_context()
        actual_id = context.get("actual_user_id") or _current_user_id()
        acting_id = context.get("acting_for_user_id") or actual_id
        decision = ExecutionAuthorization(
            actual_user_id=int(actual_id or 0),
            acting_for_user_id=int(acting_id or 0),
            action="",
            acting_permission=context.get("acting_permission"),
            formal_delegation=context.get("formal_delegation"),
            execution_context=context.get("execution_context", SELF),
        ) if actual_id else None
    if decision is None or not decision.actual_user_id:
        return {}

    acting_for_id = decision.acting_for_user_id if decision.acting_for_user_id != decision.actual_user_id else None
    values = {
        "user_id": decision.actual_user_id,
        "actual_user_id": decision.actual_user_id,
        "on_behalf_of_id": acting_for_id,
        "acting_for_user_id": acting_for_id,
        "acting_permission_id": _id(decision.acting_permission),
        "formal_delegation_id": _id(decision.formal_delegation),
        "execution_context": decision.execution_context,
        "ip_address": _request_ip(),
        "user_agent": ((request.headers.get("User-Agent", "") or "")[:500] if has_request_context() else None),
    }
    return {key: value for key, value in values.items() if value is not None}


def record_execution_audit(
    action: Any,
    *,
    decision: ExecutionAuthorization | None = None,
    actual_user_id: int | None = None,
    request_id: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    module_name: str | None = None,
    object_type: str | None = None,
    object_id: int | None = None,
    note: str | None = None,
    old_status: str | None = None,
    new_status: str | None = None,
    auto_commit: bool = False,
) -> AuditLog:
    """Create one unified audit row with actual/acting/formal identities."""
    action_code = normalize_action(action)
    fields = execution_audit_fields(decision)
    if not fields:
        actual_id = actual_user_id or _current_user_id()
        if actual_id:
            fields = {"user_id": actual_id, "actual_user_id": actual_id, "execution_context": SELF}

    target_type = object_type or target_type
    target_id = object_id if object_id is not None else target_id
    row = AuditLog(
        request_id=request_id,
        action=action_code,
        action_type=action_code,
        old_status=old_status,
        new_status=new_status,
        note=note,
        target_type=target_type,
        target_id=target_id,
        object_type=target_type,
        object_id=target_id,
        module_name=module_name,
        **fields,
    )
    db.session.add(row)
    if auto_commit:
        db.session.commit()
    return row


def notify_principal_of_execution(
    decision: ExecutionAuthorization,
    *,
    action: Any,
    target_label: str = "الإجراء",
    target_type: str | None = None,
    target_id: int | None = None,
    link_url: str | None = None,
    source: str = "workflow",
    sensitive: bool | None = None,
    auto_flush: bool = False,
) -> Notification | None:
    """Notify the principal after a delegated/acting action."""
    if not decision or int(decision.acting_for_user_id) == int(decision.actual_user_id):
        return None
    principal_id = int(decision.acting_for_user_id)
    actor = _actor_label(decision.actual_user_id)
    action_code = normalize_action(action)
    action_label = ACTION_LABELS_AR.get(action_code, target_label or action_code)
    if decision.formal_delegation:
        reference = decision.formal_delegation.decision_number or decision.formal_delegation.legal_reference or f"#{decision.formal_delegation.id}"
        message = f"قام {actor} بـ{action_label} {target_label} بموجب التفويض رقم {reference} الصادر عنك."
    else:
        message = f"قام {actor} بـ{action_label} {target_label} بالنيابة عنك."
    notification = Notification(
        user_id=principal_id,
        message=message[:255],
        type="DELEGATION_SENSITIVE" if (sensitive if sensitive is not None else action_code in SENSITIVE_ACTIONS) else "DELEGATION",
        source=(source or "workflow").strip().lower() in {"workflow", "portal"} and (source or "workflow").strip().lower() or "workflow",
        link_url=link_url,
        email_delivery_mode=("DELEGATION_SENSITIVE" if (sensitive if sensitive is not None else action_code in SENSITIVE_ACTIONS) else "GENERAL"),
        is_visible=True,
        is_read=False,
        actor_id=int(decision.actual_user_id),
    )
    db.session.add(notification)
    if auto_flush:
        db.session.flush()
    if notification.email_delivery_mode == "DELEGATION_SENSITIVE":
        # Keep email opt-in/configuration checks in the central outbox service;
        # a missing SMTP configuration must never roll back the business
        # action or the in-app notification.
        try:
            from services.notification_email import enqueue_notification_email

            enqueue_notification_email(notification)
        except Exception:
            pass
    return notification


def can_create_acting_permission(actor: Any = None, principal_user_id: Any = None) -> bool:
    actor_id = _id(actor) if actor is not None else _current_user_id()
    principal_id = _id(principal_user_id)
    if not actor_id:
        return False
    if principal_id and int(actor_id) == int(principal_id):
        return True
    user = actor if hasattr(actor, "has_perm") else db.session.get(User, actor_id)
    try:
        return bool(
            user
            and (
                user.has_role("ADMIN")
                or user.has_role("SUPER_ADMIN")
                or user.has_perm(ACTING_PERMISSION_CREATE)
                or user.has_perm(ACTING_PERMISSION_MANAGE)
            )
        )
    except Exception:
        return False


def can_create_formal_delegation(actor: Any = None, delegator_user_id: Any = None) -> bool:
    """Check the independent formal-delegation creation capability."""
    actor_id = _id(actor) if actor is not None else _current_user_id()
    if not actor_id:
        return False
    user = actor if hasattr(actor, "has_perm") else db.session.get(User, actor_id)
    try:
        if user and (user.has_role("ADMIN") or user.has_role("SUPER_ADMIN")):
            return True
        # Being the delegator is not enough; the explicit capability is
        # required, exactly as specified by can_create_delegation.
        return bool(
            user
            and (
                user.has_perm(FORMAL_DELEGATION_CREATE)
                or user.has_perm(FORMAL_DELEGATION_MANAGE)
            )
        )
    except Exception:
        return False


def acting_permission_required(module_id: str, action: str, **authorize_kwargs):
    """Decorator for routes whose action can be performed in acting mode."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            try:
                decision = authorize_action(
                    action,
                    module_id=module_id,
                    **authorize_kwargs,
                )
            except AuthorizationError:
                abort(403)
            g.current_execution_authorization = decision
            return view(*args, **kwargs)
        return wrapped
    return decorator


# Readable aliases for service callers and future modules.
check_action = authorize_action
require_action = acting_permission_required
get_current_execution_context = get_execution_context
