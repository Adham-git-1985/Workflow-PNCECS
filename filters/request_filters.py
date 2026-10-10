from datetime import datetime, timedelta
from sqlalchemy import or_, cast, String
from models import WorkflowRequest
from models import SystemSetting


DEFAULT_SLA_DAYS = 3
DEFAULT_ESCALATION_DAYS = 2
_SLA_SETTING_KEYS = ("SLA_DAYS", "ESCALATION_DAYS")


def load_sla_settings():
    """Load both SLA thresholds with one query.

    List pages call this once and reuse the returned values for filtering and
    display.  Keeping the conversion here also makes malformed settings fall
    back safely instead of breaking the whole requests page.
    """
    rows = (
        SystemSetting.query
        .filter(SystemSetting.key.in_(_SLA_SETTING_KEYS))
        .all()
    )
    values = {row.key: row.value for row in rows}

    def configured_int(key, default):
        try:
            return int(values.get(key, default))
        except (TypeError, ValueError):
            return default

    return (
        configured_int("SLA_DAYS", DEFAULT_SLA_DAYS),
        configured_int("ESCALATION_DAYS", DEFAULT_ESCALATION_DAYS),
    )


def apply_request_filters(
    query,
    args,
    *,
    sla_days=None,
    escalation_days=None,
    now=None,
):

    if args.getlist("status"):
        query = query.filter(
            WorkflowRequest.status.in_(args.getlist("status"))
        )

    if args.get("date_from"):
        query = query.filter(
            WorkflowRequest.created_at >=
            datetime.fromisoformat(args["date_from"])
        )

    if args.get("date_to"):
        query = query.filter(
            WorkflowRequest.created_at <=
            datetime.fromisoformat(args["date_to"])
        )

    if args.get("requester_id"):
        query = query.filter(
            WorkflowRequest.requester_id == args["requester_id"]
        )

    if args.get("current_role"):
        query = query.filter(
            WorkflowRequest.current_role == args["current_role"]
        )

    if args.get("priority"):
        query = query.filter(
            WorkflowRequest.priority == args["priority"]
        )

    if args.get("keyword"):
        q = f"%{args['keyword']}%"
        query = query.filter(or_(
            cast(WorkflowRequest.id, String).ilike(q),
            WorkflowRequest.title.ilike(q),
            WorkflowRequest.description.ilike(q)
        ))

    if args.get("sla_state"):
        if sla_days is None or escalation_days is None:
            loaded_sla_days, loaded_escalation_days = load_sla_settings()
            if sla_days is None:
                sla_days = loaded_sla_days
            if escalation_days is None:
                escalation_days = loaded_escalation_days

        now = now or datetime.utcnow()
        sla_deadline = now - timedelta(days=sla_days)
        esc_deadline = now - timedelta(days=sla_days + escalation_days)

        # فقط الطلبات غير النهائية
        query = query.filter(
            WorkflowRequest.status.notin_(["APPROVED", "REJECTED", "CLOSED"])
        )

        if args["sla_state"] == "ON_TRACK":
            query = query.filter(
                WorkflowRequest.created_at >= sla_deadline
            )

        elif args["sla_state"] == "BREACHED":
            query = query.filter(
                WorkflowRequest.created_at < sla_deadline,
                WorkflowRequest.created_at >= esc_deadline
            )

        elif args["sla_state"] == "ESCALATED":
            query = query.filter(
                WorkflowRequest.created_at < esc_deadline
            )

    return query

def get_sla_days():
    setting = SystemSetting.query.filter_by(key="SLA_DAYS").first()
    return int(setting.value) if setting else DEFAULT_SLA_DAYS


def get_escalation_days():
    setting = SystemSetting.query.filter_by(key="ESCALATION_DAYS").first()
    return int(setting.value) if setting else DEFAULT_ESCALATION_DAYS


def calculate_sla_state(
    request_obj,
    sla_days,
    escalation_days,
    *,
    now=None,
):
    """Calculate one request's SLA state without issuing database queries."""
    if request_obj.status in ["APPROVED", "REJECTED", "CLOSED"]:
        return None

    now = now or datetime.utcnow()
    age = now - request_obj.created_at

    if age <= timedelta(days=sla_days):
        return "ON_TRACK"
    if age <= timedelta(days=sla_days + escalation_days):
        return "BREACHED"
    return "ESCALATED"

def get_sla_state(request_obj):
    sla_days, escalation_days = load_sla_settings()
    return calculate_sla_state(request_obj, sla_days, escalation_days)
