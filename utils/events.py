from models import AuditLog, Notification, User
from extensions import db


def emit_event(
    actor_id,
    action,
    message,
    target_type=None,
    target_id=None,
    notify_user_id=None,
    notify_role=None,
    notif_type="INFO"
):
    # 1️⃣ Audit Log
    db.session.add(AuditLog(
        user_id=actor_id,
        action=action,
        note=message,
        target_type=target_type,
        target_id=target_id
    ))

    # 2️⃣ Notifications
    if notify_user_id:
        db.session.add(Notification(
            user_id=notify_user_id,
            message=message,
            type=notif_type
        ))

    if notify_role:
        users = User.query.filter_by(role=notify_role).all()
        for u in users:
            db.session.add(Notification(
                user_id=u.id,
                message=message,
                type=notif_type
            ))


    for n in notifications:
        db.session.add(n)
