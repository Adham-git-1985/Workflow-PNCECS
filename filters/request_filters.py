from datetime import datetime, timedelta
from sqlalchemy import or_, cast, String
from models import WorkflowRequest
from models import SystemSetting


def apply_request_filters(query, args):

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
        sla_days = get_sla_days()
        esc_days = get_escalation_days()

        now = datetime.utcnow()
        sla_deadline = now - timedelta(days=sla_days)
        esc_deadline = now - timedelta(days=sla_days + esc_days)

        # فقط الطلبات غير النهائية
        query = query.filter(
            WorkflowRequest.status.notin_(["APPROVED", "REJECTED"])
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
    return int(setting.value) if setting else 3


def get_escalation_days():
    setting = SystemSetting.query.filter_by(key="ESCALATION_DAYS").first()
    return int(setting.value) if setting else 2

def get_sla_state(request_obj):
    sla_days = get_sla_days()
    esc_days = get_escalation_days()

    if request_obj.status in ["APPROVED", "REJECTED"]:
        return None

    now = datetime.utcnow()
    age = now - request_obj.created_at

    if age <= timedelta(days=sla_days):
        return "ON_TRACK"
    elif age <= timedelta(days=sla_days + esc_days):
        return "BREACHED"
    else:
        return "ESCALATED"