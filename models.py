from datetime import datetime, timezone
from extensions import db
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin



class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(50), default="user")  # admin / user

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class WorkflowRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200))
    description = db.Column(db.Text)
    status = db.Column(db.String(50))
    requester_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    requester = db.relationship("User", backref="requests")
    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )
    is_escalated = db.Column(db.Boolean, default=False)


class Approval(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("workflow_request.id"))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    action = db.Column(db.String(20))  # approve / reject
    note = db.Column(db.Text)
    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )



class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    request_id = db.Column(db.Integer, db.ForeignKey("workflow_request.id"))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)

    action = db.Column(db.String(50), index=True)
    old_status = db.Column(db.String(50))
    new_status = db.Column(db.String(50))
    note = db.Column(db.Text)

    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False, index=True
    )
    user = db.relationship("User")
    request = db.relationship("WorkflowRequest")

class SystemSetting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.String(255), nullable=False)

    def __repr__(self):
        return f"<SystemSetting {self.key}={self.value}>"
