import json
import re
from collections import Counter, defaultdict
from flask import render_template, request, send_file
from flask_login import login_required, current_user
from datetime import date, datetime, timedelta
from sqlalchemy import or_, func, and_, case
from sqlalchemy.orm import aliased, joinedload

from io import BytesIO
from utils.excel import make_xlsx_bytes, make_xlsx_bytes_multi
from utils.audit_story import build_audit_story_entries
from utils.ui_labels import ui_label, ui_text

from . import audit_bp
from models import (
    AuditLog,
    User,
    WorkflowRequest,
    RequestType,
    WorkflowTemplate,
    WorkflowInstance,
    WorkflowInstanceStep,
    WorkflowStepTask,
)
from extensions import db
from permissions import roles_required
from utils.perms import perm_required


def _apply_message_visibility_filter(query):
    """Hide MESSAGE_* audit entries for non-SUPER_ADMIN."""
    role = (getattr(current_user, "role", "") or "").strip().upper()
    if role != "SUPER_ADMIN":
        query = query.filter(~AuditLog.action.like("MESSAGE_%"))
    return query


# ---------------------------------------------------------------------------
# Usage/adoption dashboard helpers
# ---------------------------------------------------------------------------
# The audit table contains both domain events (for example WORKFLOW_STARTED)
# and the application-wide request safety-net events (PAGE_VIEW and
# USER_ACTION).  These helpers deliberately treat them as activity events,
# while using the canonical actual_user_id when it is available.  That keeps
# delegation from inflating the principal's usage or changing who actually
# used the system.
_DASHBOARD_PERIODS = {
    "7": {"label": "آخر 7 أيام", "days": 7},
    "30": {"label": "آخر 30 يومًا", "days": 30},
    "90": {"label": "آخر 90 يومًا", "days": 90},
    "365": {"label": "آخر سنة", "days": 365},
    "all": {"label": "كل الفترة", "days": None},
    "custom": {"label": "فترة مخصصة", "days": None},
}

_DASHBOARD_MODULE_LABELS = {
    "WORKFLOW": "مسار",
    "CORRESPONDENCE": "المراسلات",
    "PORTAL": "البوابة",
    "HR": "الموارد البشرية",
    "ARCHIVE": "الأرشيف",
    "STORE": "المستودع",
    "TRANSPORT": "النقل",
    "INVENTORY": "المخزون والأصول",
    "MESSAGE": "الرسائل",
    "DELEGATION": "الصلاحيات والتفويض",
    "USER": "المستخدمون",
    "MEETING": "الاجتماعات",
    "SUPPORT": "الدعم الفني",
    "EVALUATION": "التقييم",
    "AUDIT": "التدقيق",
    "ADMIN": "الإدارة",
    "ACCESS": "الدخول والتصفح",
    "OTHER": "أخرى",
}

_DASHBOARD_NON_OPERATION_ACTIONS = ("PAGE_VIEW", "USER_LOGIN", "USER_LOGOUT")

# The executive view is transaction-first.  These categories intentionally
# use a small, stable vocabulary even though the audit table contains many
# domain-specific action codes.
_DASHBOARD_TRANSACTION_ACTIONS = (
    ("view", "VIEW", "اطلاع"),
    ("create", "CREATE", "إنشاء"),
    ("approve", "APPROVE", "اعتماد"),
    ("reject", "REJECT", "رفض"),
    ("reopen", "REOPEN", "إعادة فتح"),
    ("follow_up", "FOLLOW_UP", "متابعة"),
    ("mention", "MENTION", "ذكر أشخاص"),
    ("other", "OTHER", "إجراءات أخرى"),
)
_DASHBOARD_TRANSACTION_ACTION_KEYS = tuple(
    item[0] for item in _DASHBOARD_TRANSACTION_ACTIONS
)
_DASHBOARD_TRANSACTION_MODULES = ("WORKFLOW", "ADMIN_PORTAL")
_DASHBOARD_TRANSACTION_MODULE_LABELS = {
    "WORKFLOW": "مسار",
    "ADMIN_PORTAL": "البوابة الإدارية",
}
_DASHBOARD_FOLLOW_UP_ACTIONS = frozenset(
    {
        "FOLLOW_UP",
        "FOLLOWUP",
        "REQUEST_ESCALATION",
        "ESCALATED",
        "WORKFLOW_REPLY",
        "WORKFLOW_NOTE",
        "WORKFLOW_COMMENT",
        "WORKFLOW_REQUESTER_NOTE",
        "WORKFLOW_FOLLOWER_UPDATE",
        "HIERARCHY_BYPASS_FOLLOWER",
        "ASSISTANT_SECRETARY_REDIRECT_FOLLOWER",
        "CORR_FORWARD",
        "CORR_RETURN",
        "CORR_REQUEST_INFO",
        "CORR_REPLY",
    }
)


def _dashboard_actor_expression():
    """Return the canonical actor expression with legacy fallback."""
    return func.coalesce(AuditLog.actual_user_id, AuditLog.user_id)


def _dashboard_action_expression():
    """Use action_type for new rows, falling back to the historic action."""
    return func.upper(
        func.coalesce(func.nullif(AuditLog.action_type, ""), AuditLog.action)
    )


def _dashboard_module_expression():
    """Bucket audit rows into user-facing product areas.

    Historical rows predate ``module_name`` and often have it NULL.  The
    action prefix and request-audit endpoint/path therefore provide the
    fallback so old data remains useful in the adoption report.
    """
    module = func.upper(
        func.coalesce(func.nullif(AuditLog.module_name, ""), "")
    )
    action = _dashboard_action_expression()
    target = func.upper(
        func.coalesce(
            func.nullif(AuditLog.object_type, ""),
            func.nullif(AuditLog.target_type, ""),
            "",
        )
    )
    note = func.lower(func.coalesce(AuditLog.note, ""))

    return case(
        (module.like("WORKFLOW%"), "WORKFLOW"),
        (module.like("CORR%"), "CORRESPONDENCE"),
        (module.like("PORTAL%"), "PORTAL"),
        (module.like("HR%"), "HR"),
        (module.like("ARCHIVE%"), "ARCHIVE"),
        (module.like("STORE%"), "STORE"),
        (module.like("TRANSPORT%"), "TRANSPORT"),
        (module.like("INVENTORY%"), "INVENTORY"),
        (module.like("MESSAGE%"), "MESSAGE"),
        (module.like("DELEGATION%"), "DELEGATION"),
        (module.like("AUDIT%"), "AUDIT"),
        (module.like("ADMIN%"), "ADMIN"),
        (action.like("WORKFLOW%"), "WORKFLOW"),
        (action.like("STEP_%"), "WORKFLOW"),
        (action.like("PARALLEL%"), "WORKFLOW"),
        (action.like("REQUEST_%"), "WORKFLOW"),
        (action.like("CORR%"), "CORRESPONDENCE"),
        (action.like("PORTAL%"), "PORTAL"),
        (action.like("HR_%"), "HR"),
        (action.like("TIMECLK%"), "HR"),
        (action.like("ATTENDANCE%"), "HR"),
        (action.like("LEAVE%"), "HR"),
        (action.like("PAYSLIP%"), "HR"),
        (action.like("EMPLOYEE%"), "HR"),
        (action.like("ARCHIVE%"), "ARCHIVE"),
        (action.like("STORE%"), "STORE"),
        (action.like("TRANSPORT%"), "TRANSPORT"),
        (action.like("INVENTORY%"), "INVENTORY"),
        (action.like("ASSET%"), "INVENTORY"),
        (action.like("SUPPLY%"), "INVENTORY"),
        (action.like("MESSAGE%"), "MESSAGE"),
        (action.like("DELEGATION%"), "DELEGATION"),
        (action.like("MEETING%"), "MEETING"),
        (action.like("TROUBLE%"), "SUPPORT"),
        (action.like("EVALUATION%"), "EVALUATION"),
        (action.like("AUDIT%"), "AUDIT"),
        (action.like("PERMISSION%"), "USER"),
        (action.like("ROLE%"), "USER"),
        (target.like("WORKFLOW%"), "WORKFLOW"),
        (target.like("CORR%"), "CORRESPONDENCE"),
        (target.like("ARCHIVE%"), "ARCHIVE"),
        # Request-audit notes carry the endpoint/path for legacy rows.
        (note.like("%workflow.%"), "WORKFLOW"),
        (note.like("%/workflow/%"), "WORKFLOW"),
        (note.like("%portal.%"), "PORTAL"),
        (note.like("%/portal/%"), "PORTAL"),
        (note.like("%/hr/%"), "HR"),
        (note.like("%hr.%"), "HR"),
        (note.like("%/archive/%"), "ARCHIVE"),
        (note.like("%/transport/%"), "TRANSPORT"),
        (note.like("%/admin/%"), "ADMIN"),
        (action.in_(["PAGE_VIEW", "USER_LOGIN", "USER_LOGOUT", "USER_ACTION", "USER_ACTION_FAILED"]), "ACCESS"),
        else_="OTHER",
    )


def _dashboard_code(value):
    try:
        return str(value or "").strip().upper()
    except Exception:
        return ""


def _dashboard_module_label(value):
    code = _dashboard_code(value) or "OTHER"
    return _DASHBOARD_MODULE_LABELS.get(code, str(value or "أخرى"))


def _dashboard_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _dashboard_percent(value, total):
    total = _dashboard_int(total)
    if not total:
        return 0.0
    return round((_dashboard_int(value) * 100.0) / total, 1)


def _dashboard_datetime_text(value):
    if not value:
        return "—"
    try:
        return value.strftime("%Y-%m-%d %H:%M")
    except (AttributeError, ValueError):
        return str(value)


def _dashboard_usage_band(events):
    events = _dashboard_int(events)
    if events == 0:
        return "none", "لم يستخدم"
    if events <= 5:
        return "low", "استخدام محدود (1–5)"
    if events <= 20:
        return "medium", "استخدام متوسط (6–20)"
    return "high", "استخدام مرتفع (21 فأكثر)"


def _dashboard_transaction_category(action_code):
    """Map detailed audit actions to the executive transaction vocabulary.

    ``None`` means that the row is not a transaction action.  Rows with a
    transaction reference but without a known action are kept as ``other``;
    this prevents a new domain action from silently disappearing from the
    management report.
    """
    action = _dashboard_code(action_code)
    if not action:
        return None

    # Mention actions contain words such as ACCESS/REVOKED, so classify them
    # before the more general action suffixes below.
    if "MENTION" in action:
        return "mention"
    if "REOPEN" in action:
        return "reopen"
    if (
        action in {"APPROVE", "APPROVED", "APPROVAL", "STEP_APPROVED"}
        or action.endswith("_APPROVE")
        or action.endswith("_APPROVED")
        or (action.endswith("_APPROVAL") and "SUBMIT" not in action)
    ):
        return "approve"
    if (
        action in {"REJECT", "REJECTED", "STEP_REJECTED"}
        or action.endswith("_REJECT")
        or action.endswith("_REJECTED")
    ):
        return "reject"
    if (
        action in {"CREATE", "CREATED", "REQUEST_CREATED", "WORKFLOW_STARTED"}
        or action.endswith("_CREATE")
        or action.endswith("_CREATED")
    ):
        return "create"
    if action in _DASHBOARD_FOLLOW_UP_ACTIONS:
        return "follow_up"
    if (
        action in {"PAGE_VIEW", "VIEW", "READ", "OPEN", "CORR_OPEN", "REQUEST_VIEWED"}
        or action.endswith("_VIEW")
        or action.endswith("_VIEWED")
        or action.endswith("_OPEN")
        or action.endswith("_OPENED")
        or action.endswith("_READ")
    ):
        return "view"

    # Keep known transaction actions (including a future action code) visible
    # when the audit row carries a transaction reference.  The caller decides
    # whether a reference is available before using this fallback.
    return "other"


def _dashboard_transaction_module(module_code, action_code):
    """Roll detailed product buckets into the two executive product areas."""
    module = _dashboard_code(module_code)
    action = _dashboard_code(action_code)
    if module == "WORKFLOW" or action.startswith(
        ("WORKFLOW_", "STEP_", "REQUEST_", "PARALLEL_", "HIERARCHY_")
    ):
        return "WORKFLOW"
    return "ADMIN_PORTAL"


def _dashboard_filter_state():
    """Parse period/date query parameters into UTC-naive DB boundaries."""
    today = datetime.utcnow().date()
    raw_period = (request.args.get("period") or "").strip().lower()
    raw_from = (request.args.get("date_from") or "").strip()
    raw_to = (request.args.get("date_to") or "").strip()
    period = raw_period or ("custom" if raw_from or raw_to else "30")
    if period not in _DASHBOARD_PERIODS:
        period = "30"

    notice = None

    def parse_day(raw):
        if not raw:
            return None
        try:
            return date.fromisoformat(raw)
        except (TypeError, ValueError):
            return None

    if period == "all":
        date_from = None
        date_to = None
    elif period == "custom":
        date_from = parse_day(raw_from)
        date_to = parse_day(raw_to)
        if not date_from and not date_to:
            period = "30"
            date_from = today - timedelta(days=29)
            date_to = today
            notice = "لم تُحدّد فترة مخصصة؛ عُرضت آخر 30 يومًا."
    else:
        days = _DASHBOARD_PERIODS[period]["days"]
        date_from = today - timedelta(days=max(0, days - 1))
        date_to = today

    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from
        notice = "تم ترتيب تاريخي البداية والنهاية تلقائيًا."

    start_at = datetime.combine(date_from, datetime.min.time()) if date_from else None
    end_at = (
        datetime.combine(date_to + timedelta(days=1), datetime.min.time())
        if date_to
        else None
    )
    if date_from and date_to:
        summary = f"من {date_from.isoformat()} إلى {date_to.isoformat()}"
    elif date_from:
        summary = f"من {date_from.isoformat()} فصاعدًا"
    elif date_to:
        summary = f"حتى {date_to.isoformat()}"
    else:
        summary = "كل الفترة المتاحة"

    return {
        "key": period,
        "label": _DASHBOARD_PERIODS[period]["label"],
        "date_from": date_from,
        "date_to": date_to,
        "date_from_value": date_from.isoformat() if date_from else "",
        "date_to_value": date_to.isoformat() if date_to else "",
        "summary": summary,
        "start_at": start_at,
        "end_at": end_at,
        "notice": notice,
    }


def _dashboard_base_query(selected_user_id, filter_state):
    actor_expr = _dashboard_actor_expression()
    query = _apply_message_visibility_filter(AuditLog.query).filter(actor_expr.isnot(None))
    if selected_user_id:
        query = query.filter(actor_expr == int(selected_user_id))
    if filter_state["start_at"]:
        query = query.filter(AuditLog.created_at >= filter_state["start_at"])
    if filter_state["end_at"]:
        query = query.filter(AuditLog.created_at < filter_state["end_at"])
    return query


def _dashboard_user_label(user):
    if not user:
        return "مستخدم غير معروف"
    return (
        getattr(user, "full_name", None)
        or getattr(user, "name", None)
        or getattr(user, "username", None)
        or getattr(user, "email", None)
        or f"مستخدم #{getattr(user, 'id', '')}"
    )


def _build_audit_dashboard_report():
    """Build all aggregates used by the HTML and Excel usage reports."""
    filter_state = _dashboard_filter_state()
    users = User.query.order_by(User.name.asc(), User.email.asc()).all()
    users_by_id = {int(user.id): user for user in users if getattr(user, "id", None)}

    selected_user_id = request.args.get("user_id", type=int)
    if selected_user_id not in users_by_id:
        selected_user_id = None
    selected_user = users_by_id.get(selected_user_id) if selected_user_id else None

    base = _dashboard_base_query(selected_user_id, filter_state)
    actor_expr = _dashboard_actor_expression()
    action_expr = _dashboard_action_expression()
    module_expr = _dashboard_module_expression()
    day_expr = func.date(AuditLog.created_at)

    page_view_condition = action_expr == "PAGE_VIEW"
    login_condition = action_expr == "USER_LOGIN"
    logout_condition = action_expr == "USER_LOGOUT"
    failure_condition = action_expr.like("%FAILED%")
    operation_condition = ~action_expr.in_(_DASHBOARD_NON_OPERATION_ACTIONS)
    delegated_condition = or_(
        AuditLog.acting_for_user_id.isnot(None),
        AuditLog.on_behalf_of_id.isnot(None),
    )

    summary_row = base.with_entities(
        func.count(AuditLog.id).label("total_events"),
        func.count(func.distinct(actor_expr)).label("active_users"),
        func.count(func.distinct(day_expr)).label("active_days"),
        func.coalesce(func.sum(case((page_view_condition, 1), else_=0)), 0).label("page_views"),
        func.coalesce(func.sum(case((operation_condition, 1), else_=0)), 0).label("operations"),
        func.coalesce(func.sum(case((failure_condition, 1), else_=0)), 0).label("failed_operations"),
        func.coalesce(func.sum(case((login_condition, 1), else_=0)), 0).label("logins"),
        func.coalesce(func.sum(case((logout_condition, 1), else_=0)), 0).label("logouts"),
        func.coalesce(func.sum(case((delegated_condition, 1), else_=0)), 0).label("delegated_events"),
    ).first()

    summary = {
        "total_events": _dashboard_int(getattr(summary_row, "total_events", 0)),
        "active_users": _dashboard_int(getattr(summary_row, "active_users", 0)),
        "active_days": _dashboard_int(getattr(summary_row, "active_days", 0)),
        "page_views": _dashboard_int(getattr(summary_row, "page_views", 0)),
        "operations": _dashboard_int(getattr(summary_row, "operations", 0)),
        "failed_operations": _dashboard_int(getattr(summary_row, "failed_operations", 0)),
        "logins": _dashboard_int(getattr(summary_row, "logins", 0)),
        "logouts": _dashboard_int(getattr(summary_row, "logouts", 0)),
        "delegated_events": _dashboard_int(getattr(summary_row, "delegated_events", 0)),
    }

    user_stat_rows = base.with_entities(
        actor_expr.label("actor_id"),
        func.count(AuditLog.id).label("events"),
        func.count(func.distinct(day_expr)).label("active_days"),
        func.coalesce(func.sum(case((page_view_condition, 1), else_=0)), 0).label("page_views"),
        func.coalesce(func.sum(case((operation_condition, 1), else_=0)), 0).label("operations"),
        func.coalesce(func.sum(case((failure_condition, 1), else_=0)), 0).label("failed_operations"),
        func.coalesce(func.sum(case((delegated_condition, 1), else_=0)), 0).label("delegated_events"),
        func.min(AuditLog.created_at).label("first_activity"),
        func.max(AuditLog.created_at).label("last_activity"),
    ).group_by(actor_expr).all()

    stats_by_user = {}
    for row in user_stat_rows:
        actor_id = _dashboard_int(getattr(row, "actor_id", 0))
        if not actor_id:
            continue
        stats_by_user[actor_id] = {
            "events": _dashboard_int(getattr(row, "events", 0)),
            "active_days": _dashboard_int(getattr(row, "active_days", 0)),
            "page_views": _dashboard_int(getattr(row, "page_views", 0)),
            "operations": _dashboard_int(getattr(row, "operations", 0)),
            "failed_operations": _dashboard_int(getattr(row, "failed_operations", 0)),
            "delegated_events": _dashboard_int(getattr(row, "delegated_events", 0)),
            "first_activity": getattr(row, "first_activity", None),
            "last_activity": getattr(row, "last_activity", None),
        }

    module_counts = Counter()
    modules_by_user = defaultdict(Counter)
    module_stat_rows = base.with_entities(
        actor_expr.label("actor_id"),
        module_expr.label("module_code"),
        func.count(AuditLog.id).label("count"),
    ).group_by(actor_expr, module_expr).all()
    for row in module_stat_rows:
        module_code = _dashboard_code(getattr(row, "module_code", None)) or "OTHER"
        count = _dashboard_int(getattr(row, "count", 0))
        module_counts[module_code] += count
        actor_id = _dashboard_int(getattr(row, "actor_id", 0))
        if actor_id:
            modules_by_user[actor_id][module_code] += count

    action_counts = Counter()
    actions_by_user = defaultdict(Counter)
    action_stat_rows = base.with_entities(
        actor_expr.label("actor_id"),
        action_expr.label("action_code"),
        func.count(AuditLog.id).label("count"),
    ).group_by(actor_expr, action_expr).all()
    for row in action_stat_rows:
        action_code = _dashboard_code(getattr(row, "action_code", None)) or "OTHER"
        count = _dashboard_int(getattr(row, "count", 0))
        action_counts[action_code] += count
        actor_id = _dashboard_int(getattr(row, "actor_id", 0))
        if actor_id:
            actions_by_user[actor_id][action_code] += count

    def sorted_counter(counter):
        return sorted(counter.items(), key=lambda item: (-_dashboard_int(item[1]), str(item[0])))

    all_module_count = sum(module_counts.values())
    module_rows = [
        {
            "code": code,
            "label": _dashboard_module_label(code),
            "count": _dashboard_int(count),
            "share_pct": _dashboard_percent(count, all_module_count),
        }
        for code, count in sorted_counter(module_counts)
    ]

    all_action_count = sum(action_counts.values())
    action_rows = [
        {
            "code": code,
            "label": ui_label(code) or code,
            "count": _dashboard_int(count),
            "share_pct": _dashboard_percent(count, all_action_count),
        }
        for code, count in sorted_counter(action_counts)
    ]

    # Transaction-first report.  The same transaction reference is counted
    # once per user/day/module/action, even if the user opened it repeatedly
    # that day.  When an older audit row has no reference, its event count is
    # retained as an explicit fallback rather than being silently discarded.
    transaction_ref_expr = func.coalesce(
        AuditLog.request_id,
        AuditLog.object_id,
        AuditLog.target_id,
    )
    transaction_target_expr = func.upper(
        func.coalesce(
            func.nullif(AuditLog.object_type, ""),
            func.nullif(AuditLog.target_type, ""),
            "",
        )
    )
    transaction_stat_rows = (
        base.with_entities(
            day_expr.label("day"),
            actor_expr.label("actor_id"),
            module_expr.label("module_code"),
            action_expr.label("action_code"),
            transaction_target_expr.label("target_code"),
            transaction_ref_expr.label("transaction_ref"),
            func.count(AuditLog.id).label("event_count"),
        )
        .group_by(
            day_expr,
            actor_expr,
            module_expr,
            action_expr,
            transaction_target_expr,
            transaction_ref_expr,
        )
        .all()
    )

    def new_transaction_bucket():
        return {"references": set(), "unresolved_events": 0, "event_count": 0}

    transaction_buckets = defaultdict(new_transaction_bucket)
    daily_transaction_refs = defaultdict(set)
    daily_transaction_unresolved = Counter()
    user_transaction_refs = defaultdict(set)
    user_transaction_unresolved = Counter()
    module_transaction_refs = defaultdict(set)
    module_transaction_unresolved = Counter()
    module_active_users = defaultdict(set)
    module_active_days = defaultdict(set)
    transaction_user_ids = set()
    transaction_days = set()

    for row in transaction_stat_rows:
        actor_id = _dashboard_int(getattr(row, "actor_id", 0))
        if not actor_id:
            continue
        day_value = getattr(row, "day", None)
        if day_value is None:
            continue
        day_text = str(day_value)[:10]
        action_code = _dashboard_code(getattr(row, "action_code", None))
        category = _dashboard_transaction_category(action_code)
        reference = getattr(row, "transaction_ref", None)
        # A generic page view without a request/object reference is a page
        # metric, not evidence that a business transaction was viewed.
        if category == "view" and reference is None:
            continue
        # Do not turn unrelated system actions with no object into a fake
        # transaction.  Known create/decision/follow-up events are retained
        # because some historic domain writers did not populate target_id.
        if category == "other" and reference is None:
            continue
        if category is None:
            continue

        module_code = _dashboard_transaction_module(
            getattr(row, "module_code", None), action_code
        )
        target_code = _dashboard_code(getattr(row, "target_code", None))
        event_count = _dashboard_int(getattr(row, "event_count", 0))
        if not event_count:
            continue
        group_key = (day_text, actor_id, module_code, category)
        bucket = transaction_buckets[group_key]
        bucket["event_count"] += event_count

        daily_key = (day_text, actor_id, module_code)
        user_key = (actor_id, module_code)
        module_key = module_code
        transaction_user_ids.add(actor_id)
        transaction_days.add(day_text)
        module_active_users[module_key].add(actor_id)
        module_active_days[module_key].add(day_text)

        if reference is None:
            bucket["unresolved_events"] += event_count
            daily_transaction_unresolved[daily_key] += event_count
            user_transaction_unresolved[user_key] += event_count
            module_transaction_unresolved[module_key] += event_count
        else:
            identity = (target_code, reference)
            bucket["references"].add(identity)
            daily_transaction_refs[daily_key].add(identity)
            user_transaction_refs[user_key].add(identity)
            module_transaction_refs[module_key].add((actor_id, target_code, reference))

    def new_transaction_row(day_text, actor_id, module_code):
        row = {
            "day": day_text,
            "actor_id": actor_id,
            "user_name": _dashboard_user_label(users_by_id.get(actor_id)),
            "module_code": module_code,
            "module_label": _DASHBOARD_TRANSACTION_MODULE_LABELS[module_code],
            "event_count": 0,
            "transaction_interactions": 0,
            "unique_transactions": 0,
        }
        row.update({key: 0 for key in _DASHBOARD_TRANSACTION_ACTION_KEYS})
        return row

    transaction_daily_by_user_map = {}
    for (day_text, actor_id, module_code, category), bucket in transaction_buckets.items():
        daily_key = (day_text, actor_id, module_code)
        row = transaction_daily_by_user_map.setdefault(
            daily_key,
            new_transaction_row(day_text, actor_id, module_code),
        )
        count = len(bucket["references"]) + _dashboard_int(bucket["unresolved_events"])
        row[category] += count
        row["transaction_interactions"] += count
        row["event_count"] += _dashboard_int(bucket["event_count"])

    for daily_key, row in transaction_daily_by_user_map.items():
        row["unique_transactions"] = (
            len(daily_transaction_refs[daily_key])
            + _dashboard_int(daily_transaction_unresolved[daily_key])
        )

    transaction_daily_by_user = list(transaction_daily_by_user_map.values())
    transaction_daily_by_user.sort(
        key=lambda row: str(row["user_name"]).casefold()
    )
    transaction_daily_by_user.sort(
        key=lambda row: row["module_code"] != "WORKFLOW"
    )
    transaction_daily_by_user.sort(
        key=lambda row: row["day"], reverse=True
    )

    transaction_daily_map = {}
    daily_module_users = defaultdict(set)
    for row in transaction_daily_by_user:
        daily_key = (row["day"], row["module_code"])
        summary_row = transaction_daily_map.setdefault(
            daily_key,
            {
                "day": row["day"],
                "module_code": row["module_code"],
                "module_label": row["module_label"],
                "active_users": 0,
                "event_count": 0,
                "transaction_interactions": 0,
                "unique_transactions": 0,
                **{key: 0 for key in _DASHBOARD_TRANSACTION_ACTION_KEYS},
            },
        )
        daily_module_users[daily_key].add(row["actor_id"])
        for key in _DASHBOARD_TRANSACTION_ACTION_KEYS:
            summary_row[key] += _dashboard_int(row[key])
        summary_row["event_count"] += _dashboard_int(row["event_count"])
        summary_row["transaction_interactions"] += _dashboard_int(
            row["transaction_interactions"]
        )
        summary_row["unique_transactions"] += _dashboard_int(
            row["unique_transactions"]
        )

    for daily_key, row in transaction_daily_map.items():
        row["active_users"] = len(daily_module_users[daily_key])

    transaction_daily = list(transaction_daily_map.values())
    transaction_daily.sort(key=lambda row: row["module_code"] != "WORKFLOW")
    transaction_daily.sort(key=lambda row: row["day"], reverse=True)

    transaction_module_rows = []
    for module_code in _DASHBOARD_TRANSACTION_MODULES:
        category_totals = Counter()
        event_count = 0
        interaction_count = 0
        for row in transaction_daily:
            if row["module_code"] != module_code:
                continue
            for key in _DASHBOARD_TRANSACTION_ACTION_KEYS:
                category_totals[key] += _dashboard_int(row[key])
            event_count += _dashboard_int(row["event_count"])
            interaction_count += _dashboard_int(row["transaction_interactions"])
        transaction_module_rows.append(
            {
                "code": module_code,
                "label": _DASHBOARD_TRANSACTION_MODULE_LABELS[module_code],
                "unique_transactions": len(module_transaction_refs[module_code])
                + _dashboard_int(module_transaction_unresolved[module_code]),
                "transaction_interactions": interaction_count,
                "event_count": event_count,
                "active_users": len(module_active_users[module_code]),
                "active_days": len(module_active_days[module_code]),
                **{
                    key: _dashboard_int(category_totals[key])
                    for key in _DASHBOARD_TRANSACTION_ACTION_KEYS
                },
            }
        )

    transaction_user_stats = defaultdict(
        lambda: {
            "transaction_interactions": 0,
            "unique_transactions": 0,
            "workflow_transactions": 0,
            "portal_transactions": 0,
        }
    )
    for row in transaction_daily_by_user:
        stats = transaction_user_stats[row["actor_id"]]
        stats["transaction_interactions"] += _dashboard_int(
            row["transaction_interactions"]
        )
        if row["module_code"] == "WORKFLOW":
            stats["workflow_transactions"] += _dashboard_int(
                row["transaction_interactions"]
            )
        else:
            stats["portal_transactions"] += _dashboard_int(
                row["transaction_interactions"]
            )
    for (actor_id, module_code), references in user_transaction_refs.items():
        transaction_user_stats[actor_id]["unique_transactions"] += len(references)
    for (actor_id, module_code), unresolved in user_transaction_unresolved.items():
        transaction_user_stats[actor_id]["unique_transactions"] += _dashboard_int(
            unresolved
        )

    candidate_users = [selected_user] if selected_user else list(users)
    user_rows = []
    for user in candidate_users:
        user_id = int(user.id)
        stats = stats_by_user.get(user_id, {})
        transaction_stats = transaction_user_stats.get(user_id, {})
        events = _dashboard_int(stats.get("events"))
        band_key, band_label = _dashboard_usage_band(events)
        top_modules = [
            {
                "code": code,
                "label": _dashboard_module_label(code),
                "count": _dashboard_int(count),
            }
            for code, count in sorted_counter(modules_by_user.get(user_id, Counter()))[:3]
        ]
        top_actions = [
            {
                "code": code,
                "label": ui_label(code) or code,
                "count": _dashboard_int(count),
            }
            for code, count in sorted_counter(actions_by_user.get(user_id, Counter()))[:3]
        ]
        user_rows.append(
            {
                "id": user_id,
                "name": _dashboard_user_label(user),
                "email": getattr(user, "email", None) or "",
                "events": events,
                "active_days": _dashboard_int(stats.get("active_days")),
                "page_views": _dashboard_int(stats.get("page_views")),
                "operations": _dashboard_int(stats.get("operations")),
                "failed_operations": _dashboard_int(stats.get("failed_operations")),
                "delegated_events": _dashboard_int(stats.get("delegated_events")),
                "transaction_interactions": _dashboard_int(
                    transaction_stats.get("transaction_interactions")
                ),
                "unique_transactions": _dashboard_int(
                    transaction_stats.get("unique_transactions")
                ),
                "workflow_transactions": _dashboard_int(
                    transaction_stats.get("workflow_transactions")
                ),
                "portal_transactions": _dashboard_int(
                    transaction_stats.get("portal_transactions")
                ),
                "first_activity": stats.get("first_activity"),
                "last_activity": stats.get("last_activity"),
                "band_key": band_key,
                "band_label": band_label,
                "top_modules": top_modules,
                "top_actions": top_actions,
            }
        )

    if not selected_user:
        user_rows.sort(
            key=lambda row: (
                -_dashboard_int(row["transaction_interactions"]),
                -_dashboard_int(row["events"]),
                -_dashboard_int(row["active_days"]),
                str(row["name"]).casefold(),
            )
        )

    distribution_counts = Counter(row["band_key"] for row in user_rows)
    distribution = [
        {"key": "none", "label": "لم يستخدم", "count": distribution_counts["none"]},
        {"key": "low", "label": "استخدام محدود (1–5)", "count": distribution_counts["low"]},
        {"key": "medium", "label": "استخدام متوسط (6–20)", "count": distribution_counts["medium"]},
        {"key": "high", "label": "استخدام مرتفع (21 فأكثر)", "count": distribution_counts["high"]},
    ]

    trend_rows_raw = (
        base.with_entities(
            day_expr.label("day"),
            func.count(AuditLog.id).label("events"),
            func.count(func.distinct(actor_expr)).label("active_users"),
        )
        .group_by(day_expr)
        .order_by(day_expr.desc())
        .limit(31)
        .all()
    )
    trend_rows_raw.reverse()
    trend_max = max((_dashboard_int(getattr(row, "events", 0)) for row in trend_rows_raw), default=0)
    trend = []
    for row in trend_rows_raw:
        day = getattr(row, "day", None)
        day_text = str(day)[:10] if day is not None else "—"
        events = _dashboard_int(getattr(row, "events", 0))
        trend.append(
            {
                "day": day_text,
                "events": events,
                "active_users": _dashboard_int(getattr(row, "active_users", 0)),
                "bar_pct": round((events * 100.0 / trend_max), 1) if trend_max else 0,
            }
        )

    scope_users_count = 1 if selected_user else len(users)
    summary["scope_users_count"] = scope_users_count
    summary["inactive_users"] = sum(1 for row in user_rows if not row["events"])
    summary["active_rate_pct"] = _dashboard_percent(summary["active_users"], scope_users_count)
    summary["failure_rate_pct"] = _dashboard_percent(summary["failed_operations"], summary["operations"])
    summary["transaction_interactions"] = sum(
        row["transaction_interactions"] for row in transaction_module_rows
    )
    summary["unique_transactions"] = sum(
        row["unique_transactions"] for row in transaction_module_rows
    )
    summary["transaction_active_users"] = len(transaction_user_ids)
    summary["transaction_active_days"] = len(transaction_days)
    summary["workflow_transaction_interactions"] = next(
        row["transaction_interactions"]
        for row in transaction_module_rows
        if row["code"] == "WORKFLOW"
    )
    summary["portal_transaction_interactions"] = next(
        row["transaction_interactions"]
        for row in transaction_module_rows
        if row["code"] == "ADMIN_PORTAL"
    )

    return {
        "summary": summary,
        "users": user_rows,
        "user_options": [
            {
                "id": int(user.id),
                "name": _dashboard_user_label(user),
                "email": getattr(user, "email", None) or "",
            }
            for user in users
        ],
        "modules": module_rows,
        "actions": action_rows,
        "transaction_actions": [
            {"key": key, "code": code, "label": label}
            for key, code, label in _DASHBOARD_TRANSACTION_ACTIONS
        ],
        "transaction_modules": transaction_module_rows,
        "transactions_daily": transaction_daily,
        "transactions_daily_by_user": transaction_daily_by_user,
        "trend": trend,
        "distribution": distribution,
        "selected_user": selected_user,
        "selected_user_id": selected_user_id,
        "scope_label": _dashboard_user_label(selected_user) if selected_user else "كل المستخدمين",
        "period": filter_state,
        "generated_at": datetime.utcnow(),
        "generated_at_text": _dashboard_datetime_text(datetime.utcnow()),
        "print_mode": request.args.get("print") in {"1", "true", "yes"},
    }


@audit_bp.route("/")
@login_required
@roles_required("ADMIN")
def audit_index():
    page = request.args.get("page", 1, type=int)

    q = _apply_message_visibility_filter(AuditLog.query)

    pagination = (
        q.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .paginate(page=page, per_page=20, error_out=False)
    )

    # Used by shared audit templates to avoid broken request links.
    existing_request_ids = set()

    return render_template(
        "audit/index.html",
        logs=pagination.items,
        existing_request_ids=existing_request_ids,
        pagination=pagination
    )


@audit_bp.route("/logs")
@login_required
@roles_required("ADMIN")
def list_audit_logs():
    user_id = request.args.get("user_id")
    action = request.args.get("action")
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    search = request.args.get("search")

    ExecUser = aliased(User)
    BehalfUser = aliased(User)

    query = _apply_message_visibility_filter(
        AuditLog.query
        .outerjoin(ExecUser, AuditLog.user_id == ExecUser.id)
        .outerjoin(BehalfUser, AuditLog.on_behalf_of_id == BehalfUser.id)
        .options(joinedload(AuditLog.user), joinedload(AuditLog.on_behalf_of_user), joinedload(AuditLog.delegation))
    )

    if user_id:
        query = query.filter(AuditLog.user_id == user_id)

    if action:
        query = query.filter(AuditLog.action == action)

    if date_from:
        query = query.filter(
            AuditLog.created_at >= datetime.strptime(date_from, "%Y-%m-%d")
        )

    if date_to:
        query = query.filter(
            AuditLog.created_at <
            datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
        )

    if search:
        for word in search.strip().split():
            query = query.filter(
                or_(
                    AuditLog.note.ilike(f"%{word}%"),
                    AuditLog.action.ilike(f"%{word}%"),
                    or_(
                    ExecUser.email.ilike(f"%{word}%"),
                    BehalfUser.email.ilike(f"%{word}%")
                )
                )
            )

    page = request.args.get("page", 1, type=int)

    pagination = query.order_by(
        AuditLog.created_at.desc(), AuditLog.id.desc()
    ).paginate(page=page, per_page=20, error_out=False)

    users = User.query.order_by(User.email.asc()).all()
    actions = [
        row[0]
        for row in (
            _apply_message_visibility_filter(db.session.query(AuditLog.action))
            .distinct()
            .order_by(AuditLog.action.asc())
            .all()
        )
        if row[0]
    ]

    return render_template(
        "audit/list.html",
        logs=pagination.items,
        users=users,
        actions=actions,
        pagination=pagination
    )


@audit_bp.route("/dashboard")
@login_required
@perm_required("AUDIT_DASHBOARD_READ")
def audit_dashboard():
    report = _build_audit_dashboard_report()
    return render_template(
        "audit/dashboard.html",
        report=report,
        # Keep the old names available for extensions that included the
        # previous dashboard context.
        total_logs=report["summary"]["total_events"],
        top_users=[
            (row["email"] or row["name"], row["events"])
            for row in report["users"][:5]
            if row["events"]
        ],
        top_actions=[
            (row["code"], row["count"])
            for row in report["actions"][:5]
        ],
        hide_shell=report["print_mode"],
    )


@audit_bp.route("/dashboard/export-excel")
@login_required
@perm_required("AUDIT_DASHBOARD_READ")
def audit_dashboard_export_excel():
    """Export the same filtered usage report shown on the dashboard."""
    report = _build_audit_dashboard_report()
    summary = report["summary"]
    transaction_action_headers = [
        label for _key, _code, label in _DASHBOARD_TRANSACTION_ACTIONS
    ]

    sheets = [
        {
            "name": "الملخص",
            "headers": ["المؤشر", "القيمة"],
            "rows": [
                ["نطاق المستخدمين", report["scope_label"]],
                ["الفترة", report["period"]["summary"]],
                ["إجمالي السجلات", summary["total_events"]],
                ["المستخدمون ذوو النشاط", summary["active_users"]],
                ["المستخدمون بلا نشاط", summary["inactive_users"]],
                ["الأيام النشطة", summary["active_days"]],
                ["فتح الصفحات", summary["page_views"]],
                ["الإجراءات المسجلة", summary["operations"]],
                ["المحاولات غير الناجحة", summary["failed_operations"]],
                ["عمليات بالنيابة", summary["delegated_events"]],
                ["المعاملات الفريدة المتعامل معها", summary["unique_transactions"]],
                ["إجمالي تفاعلات المعاملات", summary["transaction_interactions"]],
                ["تفاعلات معاملات مسار", summary["workflow_transaction_interactions"]],
                ["تفاعلات معاملات البوابة الإدارية", summary["portal_transaction_interactions"]],
                ["المستخدمون المتعاملون مع معاملات", summary["transaction_active_users"]],
                ["أيام التعامل مع معاملات", summary["transaction_active_days"]],
                ["نسبة النشاط", f"{summary['active_rate_pct']}%"],
                ["نسبة المحاولات غير الناجحة", f"{summary['failure_rate_pct']}%"],
                ["تاريخ إعداد التقرير", report["generated_at_text"]],
            ],
        },
        {
            "name": "المستخدمون",
            "headers": [
                "المستخدم", "البريد", "كل السجلات", "فتح الصفحات",
                "الإجراءات", "غير الناجحة", "الأيام النشطة", "بالنيابة",
                "معاملات فريدة", "تفاعلات مسار", "تفاعلات البوابة",
                "الوحدات الأكثر استخدامًا", "الإجراءات الأكثر استخدامًا",
                "أول نشاط", "آخر نشاط", "التصنيف",
            ],
            "rows": [
                [
                    row["name"],
                    row["email"],
                    row["events"],
                    row["page_views"],
                    row["operations"],
                    row["failed_operations"],
                    row["active_days"],
                    row["delegated_events"],
                    row["unique_transactions"],
                    row["workflow_transactions"],
                    row["portal_transactions"],
                    "، ".join(f"{item['label']} ({item['count']})" for item in row["top_modules"]),
                    "، ".join(f"{item['label']} ({item['count']})" for item in row["top_actions"]),
                    _dashboard_datetime_text(row["first_activity"]),
                    _dashboard_datetime_text(row["last_activity"]),
                    row["band_label"],
                ]
                for row in report["users"]
            ],
        },
        {
            "name": "ملخص المعاملات",
            "headers": [
                "الوحدة", "معاملات فريدة", "إجمالي التفاعلات", *transaction_action_headers,
                "الأحداث المسجلة", "المستخدمون", "الأيام",
            ],
            "rows": [
                [
                    row["label"],
                    row["unique_transactions"],
                    row["transaction_interactions"],
                    *[row[key] for key in _DASHBOARD_TRANSACTION_ACTION_KEYS],
                    row["event_count"],
                    row["active_users"],
                    row["active_days"],
                ]
                for row in report["transaction_modules"]
            ],
        },
        {
            "name": "معاملات يومية",
            "headers": [
                "اليوم", "الوحدة", "المستخدمون", "معاملات فريدة", "إجمالي التفاعلات",
                *transaction_action_headers, "الأحداث المسجلة",
            ],
            "rows": [
                [
                    row["day"],
                    row["module_label"],
                    row["active_users"],
                    row["unique_transactions"],
                    row["transaction_interactions"],
                    *[row[key] for key in _DASHBOARD_TRANSACTION_ACTION_KEYS],
                    row["event_count"],
                ]
                for row in report["transactions_daily"]
            ],
        },
        {
            "name": "معاملات حسب المستخدم",
            "headers": [
                "اليوم", "المستخدم", "الوحدة", "معاملات فريدة", "إجمالي التفاعلات",
                *transaction_action_headers, "الأحداث المسجلة",
            ],
            "rows": [
                [
                    row["day"],
                    row["user_name"],
                    row["module_label"],
                    row["unique_transactions"],
                    row["transaction_interactions"],
                    *[row[key] for key in _DASHBOARD_TRANSACTION_ACTION_KEYS],
                    row["event_count"],
                ]
                for row in report["transactions_daily_by_user"]
            ],
        },
        {
            "name": "الوحدات",
            "headers": ["الوحدة", "عدد الاستخدام", "النسبة"],
            "rows": [[row["label"], row["count"], f"{row['share_pct']}%"] for row in report["modules"]],
        },
        {
            "name": "الإجراءات",
            "headers": ["الإجراء", "عدد الاستخدام", "النسبة"],
            "rows": [[row["label"], row["count"], f"{row['share_pct']}%"] for row in report["actions"]],
        },
        {
            "name": "النشاط اليومي",
            "headers": ["اليوم", "كل السجلات", "المستخدمون النشطون"],
            "rows": [[row["day"], row["events"], row["active_users"]] for row in report["trend"]],
        },
    ]

    xlsx_bytes = make_xlsx_bytes_multi(sheets)
    filename = f"masar_usage_report_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return send_file(
        BytesIO(xlsx_bytes),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )



@audit_bp.route("/timeline")
@login_required
@perm_required("AUDIT_TIMELINE_READ")
def system_timeline():
    """High-volume timeline with date range + pagination.

    Default: last 7 days.
    """

    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 120, type=int)
    per_page = max(50, min(per_page, 500))

    action = (request.args.get("action") or "").strip() or None
    user_id = request.args.get("user_id", type=int)
    date_from = (request.args.get("date_from") or "").strip() or None
    date_to = (request.args.get("date_to") or "").strip() or None
    days = request.args.get("days", type=int)

    request_type_id = request.args.get("request_type_id", type=int)
    template_id = request.args.get("template_id", type=int)
    step_order = request.args.get("step_order", type=int)
    view_mode = (request.args.get("view") or "timeline").strip().lower()
    if view_mode not in {"timeline", "story"}:
        view_mode = "timeline"

    # For audit logs: request could be referenced either by request_id, or by target_type/target_id.
    REQUEST_TARGET_TYPES = ["WorkflowRequest", "WORKFLOW_REQUEST", "WORKFLOWREQUEST"]

    base = _apply_message_visibility_filter(
        AuditLog.query.options(
            joinedload(AuditLog.user),
            joinedload(AuditLog.on_behalf_of_user),
            joinedload(AuditLog.delegation),
        )
    )

    if action:
        base = base.filter(AuditLog.action == action)

    if user_id:
        base = base.filter(AuditLog.user_id == user_id)

    # Default time window
    if not date_from and not date_to and not days:
        days = 7

    if days:
        base = base.filter(AuditLog.created_at >= datetime.utcnow() - timedelta(days=days))

    if date_from:
        base = base.filter(AuditLog.created_at >= datetime.strptime(date_from, "%Y-%m-%d"))

    if date_to:
        base = base.filter(
            AuditLog.created_at < datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
        )

    # Request-based filters (نوع الطلب / المسار)
    if request_type_id or template_id:
        req = aliased(WorkflowRequest)
        inst = aliased(WorkflowInstance)

        join_cond = or_(
            AuditLog.request_id == req.id,
            (AuditLog.target_type.in_(REQUEST_TARGET_TYPES) & (AuditLog.target_id == req.id)),
        )

        base = (
            base.outerjoin(req, join_cond)
                # NOTE: WorkflowRequest has no workflow_instance_id column.
                # WorkflowInstance references the request via request_id (unique).
                .outerjoin(inst, inst.request_id == req.id)
        )

        if request_type_id:
            base = base.filter(req.request_type_id == request_type_id)
        if template_id:
            base = base.filter(inst.template_id == template_id)

    # Step filter (الخطوة)
    # We support step-based audit entries coming from:
    # - WorkflowInstanceStep (target_type: WORKFLOW_STEP / WORKFLOW_INSTANCE_STEP)
    # - WorkflowStepTask   (target_type: WORKFLOW_STEP_TASK / PARALLEL_TASK)
    # - Any other entry that encodes step in note (e.g. attachments/escalations) using patterns like step=3 / Step 3 / الخطوة 3
    if step_order:
        stask = aliased(WorkflowStepTask)
        istep = aliased(WorkflowInstanceStep)

        base = base.outerjoin(
            stask,
            and_(
                AuditLog.target_type.in_(["WORKFLOW_STEP_TASK", "PARALLEL_TASK"]),
                AuditLog.target_id == stask.id,
            )
        ).outerjoin(
            istep,
            and_(
                AuditLog.target_type.in_(["WORKFLOW_STEP", "WORKFLOW_INSTANCE_STEP"]),
                AuditLog.target_id == istep.id,
            )
        )

        note_l = func.lower(AuditLog.note)
        patt_eq = f"%step={int(step_order)}%"
        patt_space = f"%step {int(step_order)}%"  # matches 'Step 3' after lower()
        patt_ar = f"%الخطوة {int(step_order)}%"

        base = base.filter(
            or_(
                stask.step_order == int(step_order),
                istep.step_order == int(step_order),
                note_l.like(patt_eq),
                note_l.like(patt_space),
                note_l.like(patt_ar),
            )
        )

    # Summary by day (for quick navigation)
    try:
        day_label = func.strftime('%Y-%m-%d', AuditLog.created_at)
        day_counts = (
            base.with_entities(day_label.label('day'), func.count(AuditLog.id))
            .group_by('day')
            .order_by(day_label.desc())
            .limit(31)
            .all()
        )
    except Exception:
        day_counts = []

    pagination = (
        base.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .paginate(page=page, per_page=per_page, error_out=False)
    )

    logs = list(pagination.items or [])

    users = User.query.order_by(User.email.asc()).all()

    # Action dropdown: keep it light
    actions_q = db.session.query(AuditLog.action).distinct().order_by(AuditLog.action)
    actions_q = _apply_message_visibility_filter(actions_q)
    actions = [a for (a,) in actions_q.limit(200).all()]

    # Dropdown data
    request_types = RequestType.query.order_by(RequestType.name_ar.asc()).all()
    templates = WorkflowTemplate.query.order_by(WorkflowTemplate.name.asc()).all()

    # Helper: effective request id
    def _effective_req_id(l: AuditLog):
        if getattr(l, 'request_id', None):
            return int(l.request_id)
        tt = (getattr(l, 'target_type', None) or '').strip()
        if l.target_id and (tt in REQUEST_TARGET_TYPES):
            try:
                return int(l.target_id)
            except Exception:
                return None
        return None

    page_request_ids = {rid for l in logs for rid in [_effective_req_id(l)] if rid}

    # Existing requests (avoid broken links)
    existing_request_ids = set()
    if page_request_ids:
        existing_request_ids = {
            int(rid) for (rid,) in db.session.query(WorkflowRequest.id)
            .filter(WorkflowRequest.id.in_(page_request_ids)).all()
        }

    # Metadata for displaying request type / template / start & end
    request_meta = {}
    if page_request_ids:
        reqs = (
            WorkflowRequest.query
            .filter(WorkflowRequest.id.in_(page_request_ids))
            .all()
        )
        instances = (
            WorkflowInstance.query
            .filter(WorkflowInstance.request_id.in_(page_request_ids))
            .all()
        )
        instance_by_request = {
            int(inst.request_id): inst
            for inst in instances
            if getattr(inst, "request_id", None) is not None
        }
        template_ids = {
            int(inst.template_id)
            for inst in instances
            if getattr(inst, "template_id", None) is not None
        }
        templates_by_id = {}
        if template_ids:
            templates_by_id = {
                int(t.id): t
                for t in WorkflowTemplate.query.filter(WorkflowTemplate.id.in_(template_ids)).all()
            }

        # start/end timestamps from audit logs (fast enough for current page)
        se_logs = (
            AuditLog.query
            .filter(
                or_(
                    AuditLog.request_id.in_(page_request_ids),
                    (AuditLog.target_type.in_(REQUEST_TARGET_TYPES) & AuditLog.target_id.in_(page_request_ids)),
                )
            )
            .filter(AuditLog.action.in_(["WORKFLOW_STARTED", "WORKFLOW_COMPLETED"]))
            .order_by(AuditLog.created_at.asc())
            .all()
        )
        started = {}
        completed = {}
        for al in se_logs:
            rid = al.request_id or (al.target_id if al.target_type in REQUEST_TARGET_TYPES else None)
            try:
                rid = int(rid) if rid is not None else None
            except Exception:
                rid = None
            if not rid:
                continue
            if al.action == "WORKFLOW_STARTED" and rid not in started:
                started[rid] = al.created_at
            if al.action == "WORKFLOW_COMPLETED":
                completed[rid] = al.created_at

        template_name_from_logs = {}
        corr_start_logs = (
            AuditLog.query
            .filter(AuditLog.request_id.in_(page_request_ids))
            .filter(AuditLog.action == "CORR_WORKFLOW_START")
            .order_by(AuditLog.created_at.asc())
            .all()
        )
        for al in corr_start_logs:
            try:
                rid = int(al.request_id)
            except Exception:
                continue
            note = str(al.note or "")
            match = re.search(r"(?:^|\s)template=([^|]+)$", note)
            if match and rid not in template_name_from_logs:
                template_name_from_logs[rid] = match.group(1).strip()

        for r in reqs:
            inst = instance_by_request.get(int(r.id))
            tpl_id = getattr(inst, "template_id", None) if inst else None
            tpl = templates_by_id.get(int(tpl_id)) if tpl_id is not None else None

            rt = getattr(r, 'request_type', None)
            request_meta[int(r.id)] = {
                "request_type": (f"{rt.code} - {rt.name_ar}" if rt else ""),
                "template_name": (tpl.name if tpl else template_name_from_logs.get(int(r.id), "")),
                "template_id": (tpl.id if tpl else (int(tpl_id) if tpl_id is not None else None)),
                "started_at": started.get(int(r.id)),
                "completed_at": completed.get(int(r.id)),
            }

    # -----------------
    # Step number for each log (used in UI and step filter UX)
    # -----------------
    def _extract_step_from_note(note: str | None):
        if not note:
            return None
        try:
            s = str(note)
        except Exception:
            return None
        # Common patterns: step=3, Step 3:, الخطوة 3
        m = re.search(r"(?:\bstep\s*=\s*|\bStep\s+)(\d+)", s, flags=re.IGNORECASE)
        if not m:
            m = re.search(r"الخطوة\s*(\d+)", s)
        if not m:
            return None
        try:
            return int(m.group(1))
        except Exception:
            return None

    log_steps = {}
    try:
        task_ids = {
            int(l.target_id) for l in logs
            if l.target_id and ((getattr(l, 'target_type', None) or '').strip() in ['WORKFLOW_STEP_TASK', 'PARALLEL_TASK'])
        }
        step_ids = {
            int(l.target_id) for l in logs
            if l.target_id and ((getattr(l, 'target_type', None) or '').strip() in ['WORKFLOW_STEP', 'WORKFLOW_INSTANCE_STEP'])
        }

        task_step_map = {}
        if task_ids:
            for tid, so in db.session.query(WorkflowStepTask.id, WorkflowStepTask.step_order).filter(WorkflowStepTask.id.in_(task_ids)).all():
                task_step_map[int(tid)] = int(so) if so is not None else None

        inst_step_map = {}
        if step_ids:
            for sid, so in db.session.query(WorkflowInstanceStep.id, WorkflowInstanceStep.step_order).filter(WorkflowInstanceStep.id.in_(step_ids)).all():
                inst_step_map[int(sid)] = int(so) if so is not None else None

        for l in logs:
            tt = ((getattr(l, 'target_type', None) or '').strip())
            st = None
            if tt in ['WORKFLOW_STEP_TASK', 'PARALLEL_TASK'] and l.target_id:
                st = task_step_map.get(int(l.target_id))
            elif tt in ['WORKFLOW_STEP', 'WORKFLOW_INSTANCE_STEP'] and l.target_id:
                st = inst_step_map.get(int(l.target_id))

            if st is None:
                st = _extract_step_from_note(getattr(l, 'note', None))

            if st is not None:
                log_steps[int(l.id)] = int(st)
    except Exception:
        log_steps = {}

    story_entries = []
    if view_mode == "story":
        story_entries = build_audit_story_entries(
            logs,
            request_meta=request_meta,
            log_steps=log_steps,
        )

    return render_template(
        "audit/timeline.html",
        logs=logs,
        story_entries=story_entries,
        view_mode=view_mode,
        pagination=pagination,
        users=users,
        actions=actions,
        request_types=request_types,
        templates=templates,
        day_counts=day_counts,
        existing_request_ids=existing_request_ids,
        request_meta=request_meta,
        request_times=request_meta,
        log_steps=log_steps,
        req_target_types=REQUEST_TARGET_TYPES,
        filters={
            "action": action or "",
            "user_id": user_id or "",
            "date_from": date_from or "",
            "date_to": date_to or "",
            "days": days or "",
            "per_page": per_page,
            "request_type_id": request_type_id or "",
            "template_id": template_id or "",
            "step_order": step_order or "",
            "view": view_mode,
        }
    )




@audit_bp.route("/request/<int:request_id>/snapshot")
@login_required
@perm_required("AUDIT_TIMELINE_READ")
def deleted_request_snapshot(request_id):
    """Show snapshot and audit history for a request that was deleted.

    The deletion event stores a JSON snapshot inside AuditLog.note (SNAPSHOT_JSON:...).
    """
    # If the request still exists, redirect to the normal view
    if WorkflowRequest.query.get(request_id):
        # Use workflow view if available
        try:
            from flask import redirect, url_for
            return redirect(url_for("workflow.view_request", request_id=request_id))
        except Exception:
            pass

    del_log = (
        AuditLog.query
        .options(
            joinedload(AuditLog.user),
            joinedload(AuditLog.on_behalf_of_user),
            joinedload(AuditLog.delegation),
        )
        .filter(AuditLog.action == "REQUEST_DELETED")
        .filter(AuditLog.target_type.in_(["WorkflowRequest","WORKFLOW_REQUEST"]))
        .filter(AuditLog.target_id == request_id)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .first()
    )

    snapshot = None
    if del_log and del_log.note and "SNAPSHOT_JSON:" in del_log.note:
        raw = del_log.note.split("SNAPSHOT_JSON:", 1)[1].strip()
        try:
            snapshot = json.loads(raw)
        except Exception:
            snapshot = None

    logs = (
        AuditLog.query
        .options(
            joinedload(AuditLog.user),
            joinedload(AuditLog.on_behalf_of_user),
            joinedload(AuditLog.delegation),
        )
        .filter(AuditLog.target_type.in_(["WorkflowRequest","WORKFLOW_REQUEST"]))
        .filter(AuditLog.target_id == request_id)
        .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        .all()
    )

    return render_template(
        "audit/deleted_request_snapshot.html",
        request_id=request_id,
        del_log=del_log,
        snapshot=snapshot,
        logs=logs,
    )


@audit_bp.route("/timeline/export.xlsx")
@login_required
@perm_required("AUDIT_TIMELINE_READ")
def system_timeline_export_excel():
    """Export timeline to Excel honoring the same filters."""
    action = (request.args.get("action") or "").strip() or None
    user_id = request.args.get("user_id", type=int)
    date_from = (request.args.get("date_from") or "").strip() or None
    date_to = (request.args.get("date_to") or "").strip() or None
    days = request.args.get("days", type=int)
    request_type_id = request.args.get("request_type_id", type=int)
    template_id = request.args.get("template_id", type=int)
    step_order = request.args.get("step_order", type=int)

    REQUEST_TARGET_TYPES = ["WorkflowRequest", "WORKFLOW_REQUEST", "WORKFLOWREQUEST"]

    q = _apply_message_visibility_filter(
        AuditLog.query.options(
            joinedload(AuditLog.user),
            joinedload(AuditLog.on_behalf_of_user),
            joinedload(AuditLog.delegation),
        )
    )

    if action:
        q = q.filter(AuditLog.action == action)
    if user_id:
        q = q.filter(AuditLog.user_id == user_id)

    if not date_from and not date_to and not days:
        days = 7
    if days:
        q = q.filter(AuditLog.created_at >= datetime.utcnow() - timedelta(days=days))
    if date_from:
        q = q.filter(AuditLog.created_at >= datetime.strptime(date_from, "%Y-%m-%d"))
    if date_to:
        q = q.filter(AuditLog.created_at < datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1))

    if request_type_id or template_id:
        req = aliased(WorkflowRequest)
        inst = aliased(WorkflowInstance)
        join_cond = or_(
            AuditLog.request_id == req.id,
            (AuditLog.target_type.in_(REQUEST_TARGET_TYPES) & (AuditLog.target_id == req.id)),
        )
        # NOTE: WorkflowRequest has no workflow_instance_id column.
        q = q.outerjoin(req, join_cond).outerjoin(inst, inst.request_id == req.id)
        if request_type_id:
            q = q.filter(req.request_type_id == request_type_id)
        if template_id:
            q = q.filter(inst.template_id == template_id)

    # Step filter (الخطوة)
    if step_order:
        stask = aliased(WorkflowStepTask)
        istep = aliased(WorkflowInstanceStep)

        q = q.outerjoin(
            stask,
            and_(
                AuditLog.target_type.in_(["WORKFLOW_STEP_TASK", "PARALLEL_TASK"]),
                AuditLog.target_id == stask.id,
            )
        ).outerjoin(
            istep,
            and_(
                AuditLog.target_type.in_(["WORKFLOW_STEP", "WORKFLOW_INSTANCE_STEP"]),
                AuditLog.target_id == istep.id,
            )
        )

        note_l = func.lower(AuditLog.note)
        patt_eq = f"%step={int(step_order)}%"
        patt_space = f"%step {int(step_order)}%"
        patt_ar = f"%الخطوة {int(step_order)}%"
        q = q.filter(
            or_(
                stask.step_order == int(step_order),
                istep.step_order == int(step_order),
                note_l.like(patt_eq),
                note_l.like(patt_space),
                note_l.like(patt_ar),
            )
        )

    logs = q.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(20000).all()

    # Collect request ids
    def _effective_req_id(l: AuditLog):
        if getattr(l, 'request_id', None):
            return int(l.request_id)
        tt = (getattr(l, 'target_type', None) or '').strip()
        if l.target_id and (tt in REQUEST_TARGET_TYPES):
            try:
                return int(l.target_id)
            except Exception:
                return None
        return None

    req_ids = {rid for l in logs for rid in [_effective_req_id(l)] if rid}

    request_meta = {}
    if req_ids:
        reqs = WorkflowRequest.query.filter(WorkflowRequest.id.in_(req_ids)).all()

        se_logs = (
            AuditLog.query
            .filter(
                or_(
                    AuditLog.request_id.in_(req_ids),
                    (AuditLog.target_type.in_(REQUEST_TARGET_TYPES) & AuditLog.target_id.in_(req_ids)),
                )
            )
            .filter(AuditLog.action.in_(["WORKFLOW_STARTED", "WORKFLOW_COMPLETED"]))
            .order_by(AuditLog.created_at.asc())
            .all()
        )
        started = {}
        completed = {}
        for al in se_logs:
            rid = al.request_id or (al.target_id if al.target_type in REQUEST_TARGET_TYPES else None)
            try:
                rid = int(rid) if rid is not None else None
            except Exception:
                rid = None
            if not rid:
                continue
            if al.action == "WORKFLOW_STARTED" and rid not in started:
                started[rid] = al.created_at
            if al.action == "WORKFLOW_COMPLETED":
                completed[rid] = al.created_at

        for r in reqs:
            tpl = None
            try:
                tpl = r.workflow_instance.template if r.workflow_instance else None
            except Exception:
                tpl = None
            rt = getattr(r, 'request_type', None)
            request_meta[int(r.id)] = {
                "request_type": (f"{rt.code} - {rt.name_ar}" if rt else ""),
                "template_name": (tpl.name if tpl else ""),
                "started_at": started.get(int(r.id)),
                "completed_at": completed.get(int(r.id)),
            }

    # Step number map for export (batch)
    def _extract_step_from_note(note):
        if not note:
            return None
        try:
            s = str(note)
        except Exception:
            return None

        # Common patterns: step=3, Step 3, الخطوة 3
        m = re.search(r"(?:\bstep\s*=\s*|\bStep\s+)(\d+)", s, flags=re.IGNORECASE)
        if not m:
            m = re.search(r"الخطوة\s*(\d+)", s)
        if not m:
            return None
        try:
            return int(m.group(1))
        except Exception:
            return None

    task_ids = {
        int(l.target_id) for l in logs
        if l.target_id and ((getattr(l, 'target_type', None) or '').strip() in ['WORKFLOW_STEP_TASK', 'PARALLEL_TASK'])
    }
    step_ids = {
        int(l.target_id) for l in logs
        if l.target_id and ((getattr(l, 'target_type', None) or '').strip() in ['WORKFLOW_STEP', 'WORKFLOW_INSTANCE_STEP'])
    }

    task_step_map = {}
    if task_ids:
        for tid, so in (
            db.session.query(WorkflowStepTask.id, WorkflowStepTask.step_order)
            .filter(WorkflowStepTask.id.in_(task_ids))
            .all()
        ):
            task_step_map[int(tid)] = int(so) if so is not None else None

    inst_step_map = {}
    if step_ids:
        for sid, so in (
            db.session.query(WorkflowInstanceStep.id, WorkflowInstanceStep.step_order)
            .filter(WorkflowInstanceStep.id.in_(step_ids))
            .all()
        ):
            inst_step_map[int(sid)] = int(so) if so is not None else None

    headers = [
        "المعرف",
        "التاريخ والوقت",
        "الإجراء",
        "المستخدم",
        "نيابة عن",
        "رقم الطلب",
        "نوع الطلب",
        "المسار / النموذج",
        "رقم المرحلة",
        "بداية سير العمل",
        "اكتمال سير العمل",
        "نوع الهدف",
        "رقم الهدف",
        "الملاحظات",
    ]

    rows = []
    for l in logs:
        rid = _effective_req_id(l)
        meta = request_meta.get(rid or -1, {})

        # Resolve step number if possible
        st = None
        tt = ((getattr(l, 'target_type', None) or '').strip())
        if tt in ['WORKFLOW_STEP_TASK', 'PARALLEL_TASK'] and l.target_id:
            st = task_step_map.get(int(l.target_id))
        elif tt in ['WORKFLOW_STEP', 'WORKFLOW_INSTANCE_STEP'] and l.target_id:
            st = inst_step_map.get(int(l.target_id))
        if st is None:
            st = _extract_step_from_note(getattr(l, 'note', None))

        rows.append([
            l.id,
            l.created_at.strftime('%Y-%m-%d %H:%M:%S') if l.created_at else '',
            ui_label(l.action),
            (l.user.email if l.user else 'System'),
            (l.on_behalf_of_user.email if l.on_behalf_of_user else ''),
            rid or '',
            meta.get('request_type', ''),
            meta.get('template_name', ''),
            (st if st is not None else ''),
            (meta.get('started_at').strftime('%Y-%m-%d %H:%M:%S') if meta.get('started_at') else ''),
            (meta.get('completed_at').strftime('%Y-%m-%d %H:%M:%S') if meta.get('completed_at') else ''),
            ui_label(l.target_type) if l.target_type else '',
            l.target_id or '',
            ui_text(l.note) if l.note else '',
        ])

    content = make_xlsx_bytes("Timeline", headers, rows)
    filename = f"system_timeline_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.xlsx"

    return send_file(
        BytesIO(content),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )
