from datetime import datetime, timedelta
from models import WorkflowRequest, AuditLog, SystemSetting
from extensions import db
from filters.request_filters import get_sla_days, get_escalation_days

FINAL_STATUSES = ["APPROVED", "REJECTED"]
THROTTLE_MINUTES = 10


def _get_setting(key):
    s = SystemSetting.query.filter_by(key=key).first()
    return s.value if s else None


def _set_setting(key, value):
    s = SystemSetting.query.filter_by(key=key).first()
    if s:
        s.value = value
    else:
        s = SystemSetting(key=key, value=value)
        db.session.add(s)

    db.session.flush()   # يضمن توليد id
    db.session.commit()



def run_escalation_if_needed():
    now = datetime.utcnow()

    last_run_raw = _get_setting("ESCALATION_LAST_RUN")
    if last_run_raw:
        last_run = datetime.fromisoformat(last_run_raw)
        if now - last_run < timedelta(minutes=THROTTLE_MINUTES):
            return  # throttle

    esc_deadline = now - timedelta(
        days=get_sla_days() + get_escalation_days()
    )

    escalated_requests = (
        WorkflowRequest.query
        .filter(
            WorkflowRequest.status.notin_(FINAL_STATUSES),
            WorkflowRequest.created_at < esc_deadline,
            WorkflowRequest.escalated_at.is_(None)
        )
        .all()
    )

    for req in escalated_requests:
        req.status = "ESCALATED"
        req.escalated_at = now

        db.session.add(AuditLog(
            request_id=req.id,
            action="ESCALATED",
            note="Request exceeded SLA and escalation threshold",
            created_at=now
        ))

    if escalated_requests:
        db.session.commit()

    _set_setting("ESCALATION_LAST_RUN", now.isoformat())
