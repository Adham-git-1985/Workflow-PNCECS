from datetime import datetime, timedelta
from models import WorkflowRequest, AuditLog
from extensions import db
from filters.request_filters import get_sla_days, get_escalation_days

def run_escalation_check():

    now = datetime.utcnow()
    esc_deadline = now - timedelta(
        days=get_sla_days() + get_escalation_days()
    )

    escalated_requests = WorkflowRequest.query.filter(
        WorkflowRequest.status.notin_(["APPROVED", "REJECTED"]),
        WorkflowRequest.created_at < esc_deadline,
        WorkflowRequest.escalated_at.is_(None)   # إن وجد العمود
    ).all()

    for req in escalated_requests:
        log = AuditLog(
            request_id=req.id,
            action="ESCALATED",
            note="Request exceeded SLA and escalation threshold",
            created_at=now
        )
        db.session.add(log)

        # Optional column
        if hasattr(req, "escalated_at"):
            req.escalated_at = now

    db.session.commit()
