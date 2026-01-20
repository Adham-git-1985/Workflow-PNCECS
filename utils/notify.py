from models import Notification, AuditLog
from extensions import db


def notify_and_audit(
    *,
    actor_id,
    target_user_id,
    action,
    message,
    target_type=None,
    target_id=None
):
    # Notification
    notif = Notification(
        user_id=target_user_id,
        message=message
    )

    # Audit
    audit = AuditLog(
        action=action,
        user_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        description=message
    )

    db.session.add(notif)
    db.session.add(audit)

def notify_and_audit(
    *,
    actor_id,
    message,
    action,
    target_user_id=None,
    role=None,
    notif_type="INFO",
    target_type=None,
    target_id=None
):
    notifications = []

    if role:
        users = User.query.filter_by(role=role).all()
        for u in users:
            notifications.append(Notification(
                user_id=u.id,
                message=message,
                type=notif_type,
                role=role
            ))
    else:
        notifications.append(Notification(
            user_id=target_user_id,
            message=message,
            type=notif_type
        ))

    audit = AuditLog(
        action=action,
        user_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        description=message
    )

    for n in notifications:
        db.session.add(n)

    db.session.add(audit)