from datetime import datetime
from extensions import db
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin


class User(db.Model, UserMixin):
    __tablename__ = "user"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(50), index=True)
    department_id = db.Column(db.Integer, nullable=True)


    def has_role(self, role_name):
        return self.role == role_name


    # =====================
    # Password helpers
    # =====================
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def unread_notifications_count(self):
        return Notification.query.filter_by(
            user_id=self.id,
            is_read=False
        ).count()


class WorkflowRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200))
    description = db.Column(db.Text)
    status = db.Column(db.String(50))

    requester_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    requester = db.relationship("User", backref="requests")

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    is_escalated = db.Column(db.Boolean, default=False)
    escalated_at = db.Column(db.DateTime, nullable=True)
    current_role = db.Column(db.String(50), default="dept_head")



class Approval(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("workflow_request.id"))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))

    action = db.Column(db.String(20))
    note = db.Column(db.Text)

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False
    )


class AuditLog(db.Model):
    __tablename__ = "audit_log"
    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("workflow_request.id"))
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id"),
        nullable=True
    )
    note = db.Column(db.Text, nullable=True)
    action = db.Column(db.String(100), nullable=False)
    old_status = db.Column(db.String(50))
    new_status = db.Column(db.String(50))
    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False,
        index=True
    )
    user = db.relationship("User")
    request = db.relationship("WorkflowRequest")
    target_type = db.Column(db.String(50))
    target_id = db.Column(db.Integer)


class SystemSetting(db.Model):
    __tablename__ = "system_setting"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.String(255), nullable=True)

# ======================
# Archive Models
# ======================
class ArchivedFile(db.Model):
    __tablename__ = "archived_file"

    id = db.Column(db.Integer, primary_key=True)

    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)

    upload_date = db.Column(db.DateTime, default=datetime.utcnow)

    file_path = db.Column(db.String(500), nullable=False)
    mime_type = db.Column(db.String(100))
    file_size = db.Column(db.Integer)

    visibility = db.Column(db.String(30), default="owner")

    owner_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id"),
        nullable=False
    )

    deleted_by = db.Column(
        db.Integer,
        db.ForeignKey("user.id"),
        nullable=True
    )

    is_deleted = db.Column(db.Boolean, default=False)
    deleted_at = db.Column(db.DateTime)

    owner = db.relationship(
        "User",
        foreign_keys=[owner_id],
        backref="archived_files"
    )

    deleted_by_user = db.relationship(
        "User",
        foreign_keys=[deleted_by],
        backref="deleted_archived_files"
    )
    department_id = db.Column(db.Integer, nullable=True)
    is_signed = db.Column(db.Boolean, default=False)
    signed_at = db.Column(db.DateTime)
    signed_by = db.Column(db.Integer, db.ForeignKey("user.id"))


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    message = db.Column(db.String(255))
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    type = db.Column(db.String(50), default="INFO")
    role = db.Column(db.String(50), nullable=True)


class FilePermission(db.Model):
    __tablename__ = "file_permission"

    id = db.Column(db.Integer, primary_key=True)

    file_id = db.Column(
        db.Integer,
        db.ForeignKey("archived_file.id"),
        nullable=False
    )

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id"),
        nullable=False
    )

    file = db.relationship(
        "ArchivedFile",
        backref="shared_with"
    )

    user = db.relationship(
        "User",
        backref="shared_files"
    )


workflow_request_id = db.Column(
    db.Integer,
    db.ForeignKey("workflow_request.id"),
    nullable=True
)

workflow_request = db.relationship(
    "WorkflowRequest",
    backref="attachments"
)


class RolePermission(db.Model):
    __tablename__ = "role_permission"

    id = db.Column(db.Integer, primary_key=True)
    role = db.Column(db.String(50), nullable=False)
    permission = db.Column(db.String(100), nullable=False)

    __table_args__ = (
        db.UniqueConstraint("role", "permission", name="uix_role_permission"),
    )
