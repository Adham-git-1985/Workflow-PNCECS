from flask import current_app
from models import AuditLog
from extensions import db
from flask_mail import Message

def send_escalation_email(mail, user_email, request_id):

    # Prevent duplicate notifications
    already_notified = AuditLog.query.filter_by(
        request_id=request_id,
        action="ESCALATION_EMAIL_SENT"
    ).first()

    if already_notified:
        return

    msg = Message(
        subject="🚨 Workflow Request Escalated",
        recipients=[user_email],
        body=f"""
A workflow request (ID: {request_id}) has been escalated
due to SLA breach.

Please review it as soon as possible.
"""
    )

    mail.send(msg)

    db.session.add(AuditLog(
        request_id=request_id,
        action="ESCALATION_EMAIL_SENT",
        note="Escalation email sent"
    ))
    db.session.commit()
