from datetime import datetime, timedelta
from app import create_app
from extensions import db
from models import (
    AuditLog,
    SystemSetting,
    WorkflowInstance,
    WorkflowInstanceStep,
    WorkflowRequest,
)

FINAL_STATUSES = ["APPROVED", "REJECTED"]

ESCALATION_ROLE_MAP = {
    "dept_head": "secretary_general",
    "finance": "secretary_general"
}

SYSTEM_USER_ID = None


def _has_suspended_current_step(request_row) -> bool:
    instance = WorkflowInstance.query.filter_by(request_id=request_row.id).first()
    if not instance:
        return False
    step = WorkflowInstanceStep.query.filter_by(
        instance_id=instance.id,
        step_order=instance.current_step_order,
    ).first()
    try:
        return int(getattr(step, "sla_days", None)) == -1
    except (TypeError, ValueError):
        return False


def get_setting(key, default):
    setting = SystemSetting.query.filter_by(key=key).first()
    return int(setting.value) if setting else default


def run_escalation():
    app = create_app()

    with app.app_context():

        SLA_DAYS = get_setting("SLA_DAYS", 3)
        ESCALATION_DAYS = get_setting("ESCALATION_DAYS", 2)

        now = datetime.utcnow()
        escalation_threshold = now - timedelta(
            days=SLA_DAYS + ESCALATION_DAYS
        )

        escalated_requests = (
            WorkflowRequest.query
            .filter(
                WorkflowRequest.status.notin_(FINAL_STATUSES),
                WorkflowRequest.created_at <= escalation_threshold,
                WorkflowRequest.is_escalated == False
            )
            .all()
        )

        escalated_count = 0
        for req in escalated_requests:
            if _has_suspended_current_step(req):
                continue
            old_status = req.status
            old_role = req.current_role or "dept_head"

            req.is_escalated = True
            req.escalated_at = now
            req.status = "ESCALATED"

            new_role = ESCALATION_ROLE_MAP.get(
                old_role,
                "secretary_general"
            )
            req.current_role = new_role

            log = AuditLog(
                request_id=req.id,
                user_id=SYSTEM_USER_ID,
                action="ESCALATION",
                old_status=old_status,
                new_status="ESCALATED",
                note=(
                    f"Escalated from {old_role} to {new_role} "
                    f"after {SLA_DAYS + ESCALATION_DAYS} days"
                )
            )
            db.session.add(log)
            escalated_count += 1

        if escalated_count:
            db.session.commit()
            print(f"✔ Escalated {escalated_count} requests")
        else:
            print("ℹ No requests to escalate")


if __name__ == "__main__":
    run_escalation()
