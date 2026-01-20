from datetime import datetime
from extensions import db
from models import Notification, User


def emit_event(
    actor_id,
    action,
    message,
    target_type=None,
    target_id=None,
    notify_user_id=None,
    notify_role=None,
    level="INFO"
):
    # ✅ تعريف المتغير (كان مفقود فعليًا عندك)
    notifications = []

    #إشعار لمستخدم محدد
    if notify_user_id:
        notifications.append(
            Notification(
                user_id=notify_user_id,
                message=message,
                type=level,
                is_read=False,
                created_at=datetime.utcnow()
            )
        )

    #  إشعار حسب الدور
    if notify_role:
        users = User.query.filter_by(role=notify_role).all()

        for u in users:
            notifications.append(
                Notification(
                    user_id=u.id,
                    message=message,
                    type=level,
                    is_read=False,
                    created_at=datetime.utcnow()
                )
            )

    #  حفظ
    if notifications:
        db.session.add_all(notifications)
        db.session.commit()
