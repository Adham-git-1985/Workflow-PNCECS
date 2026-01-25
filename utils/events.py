from datetime import datetime
import uuid
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
    level="INFO",
    notif_type=None,     # alias قديم
    track_for_actor=False,  # ✅ read-receipt style tracking for sender
    auto_commit=True,    # ✅ تحكم بالـ commit
    **kwargs
):
    # لو حد استعمل notif_type بالغلط، اعتبرها level
    if notif_type is not None:
        level = notif_type

    now = datetime.utcnow()
    event_key = uuid.uuid4().hex

    # ✅ منع التكرار (مثلاً notify_user_id ضمن نفس الدور)
    user_ids = set()

    if notify_user_id:
        user_ids.add(int(notify_user_id))

    if notify_role:
        role_user_ids = (
            db.session.query(User.id)
            .filter(User.role == notify_role)
            .all()
        )
        for (uid,) in role_user_ids:
            user_ids.add(int(uid))

    if not user_ids:
        return

    notifications = []

    # Recipient notifications
    for uid in user_ids:
        notifications.append(
            Notification(
                user_id=uid,
                message=message,
                type=level,
                is_read=False,
                created_at=now,
                actor_id=actor_id,
                event_key=event_key,
                is_mirror=False,
            )
        )

    # Sender mirror notification (shows "unread" until recipients read)
    if track_for_actor and actor_id and int(actor_id) not in user_ids:
        notifications.append(
            Notification(
                user_id=int(actor_id),
                message=f"متابعة: {message}",
                type=level,
                is_read=False,
                created_at=now,
                actor_id=int(actor_id),
                event_key=event_key,
                is_mirror=True,
            )
        )

    db.session.add_all(notifications)

    if auto_commit:
        db.session.commit()
