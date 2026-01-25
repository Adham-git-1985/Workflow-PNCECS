from datetime import datetime, timedelta

from extensions import db
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin


# ======================
# Users
# ======================
class User(db.Model, UserMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(50), index=True)
    department_id = db.Column(db.Integer, nullable=True)
    permissions = db.relationship(
        "UserPermission",
        backref="user",
        lazy="selectin",
        cascade="all, delete-orphan"
    )

    def has_perm(self, key: str) -> bool:
        """Permission check with CRUD support + backward compatibility.

        Supported patterns:
          - <MODULE>_<ACTION> where ACTION in {READ, CREATE, UPDATE, DELETE}
          - Legacy: <MODULE>_MANAGE

        Backward compatibility rules:
          - If user has <MODULE>_MANAGE → all CRUD actions are allowed.
          - If user has all four CRUD actions → <MODULE>_MANAGE is treated as allowed.
        """
        if self.has_role("ADMIN"):
            return True

        key = (key or "").strip().upper()
        if not key:
            return False

        perms = [
            (p.key or "").strip().upper()
            for p in (self.permissions or [])
            if getattr(p, "is_allowed", False)
        ]

        if key in perms:
            return True

        actions = ("READ", "CREATE", "UPDATE", "DELETE")

        # CRUD → MANAGE
        for act in actions:
            suffix = "_" + act
            if key.endswith(suffix):
                base = key[: -len(suffix)]
                return f"{base}_MANAGE" in perms

        # MANAGE → CRUD-all
        if key.endswith("_MANAGE"):
            base = key[: -len("_MANAGE")]
            return all(f"{base}_{act}" in perms for act in actions)

        return False


    def has_role(self, role_name):
        role_name = (role_name or "").strip().upper()
        my_role = (self.role or "").strip().upper()

        # SUPER_ADMIN inherits ADMIN privileges
        if my_role == "SUPER_ADMIN" and role_name == "ADMIN":
            return True

        return my_role == role_name

    @property
    def full_name(self):
        return self.email or f"User #{self.id}"

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def unread_notifications_count(self):
        # يتم تنفيذها وقت الاستدعاء، فمسموح حتى لو Notification أسفل الملف
        return Notification.query.filter_by(
            user_id=self.id,
            is_read=False
        ).count()


# ======================
# Workflow Core
# ======================
class WorkflowRequest(db.Model):
    # NOTE: no __tablename__ => default table name will be "workflow_request"
    id = db.Column(db.Integer, primary_key=True)

    title = db.Column(db.String(200))
    description = db.Column(db.Text)
    status = db.Column(db.String(50))

    requester_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    requester = db.relationship("User", backref="requests")

    # Request Type (optional)
    request_type_id = db.Column(db.Integer, db.ForeignKey("request_types.id"), nullable=True)
    request_type = db.relationship("RequestType")

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    is_escalated = db.Column(db.Boolean, default=False)
    escalated_at = db.Column(db.DateTime, nullable=True)

    current_role = db.Column(db.String(50), default="dept_head")

    # Runtime instance (مسار منفّذ فعلياً)
    workflow_instance = db.relationship(
        "WorkflowInstance",
        backref="request",
        uselist=False
    )

    # Attachments linking to archive
    attachments = db.relationship(
        "RequestAttachment",
        backref="request",
        cascade="all, delete-orphan"
    )


class Approval(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("workflow_request.id"))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    action = db.Column(db.String(20))
    note = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class AuditLog(db.Model):
    __tablename__ = "audit_log"

    id = db.Column(db.Integer, primary_key=True)

    request_id = db.Column(db.Integer, db.ForeignKey("workflow_request.id"))

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
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

    # target reference (موجود عندك)
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
        db.ForeignKey("users.id"),
        nullable=False
    )

    deleted_by = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
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

    signed_by = db.Column(db.Integer, db.ForeignKey("users.id"))

    @property
    def file_type(self):
        name = self.original_name or self.stored_name or ""
        if "." in name:
            ext = name.rsplit(".", 1)[1].upper()
            if ext == "JPEG":
                ext = "JPG"
            return ext
        if self.mime_type:
            mt = (self.mime_type or "").lower()
            if "pdf" in mt:
                return "PDF"
            if "png" in mt:
                return "PNG"
            if "jpeg" in mt or "jpg" in mt:
                return "JPG"
        return ""


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
        db.ForeignKey("users.id"),
        nullable=False
    )

    file = db.relationship(
        "ArchivedFile",
        backref="shared_with"   # list of FilePermission
    )

    # NOTE:
    # file_permission has TWO FKs to users (user_id, shared_by) so SQLAlchemy
    # needs an explicit foreign_keys to avoid AmbiguousForeignKeysError.
    user = db.relationship(
        "User",
        foreign_keys=[user_id],
        backref=db.backref(
            "shared_files",
            lazy="selectin",
            foreign_keys="FilePermission.user_id",
        ),
        lazy="joined",
    )

    can_download = db.Column(db.Boolean, default=True)
    can_share = db.Column(db.Boolean, default=False)
    delegated_by = db.Column(db.Integer, nullable=True)  # user_id
    shared_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    shared_by_user = db.relationship(
        "User",
        foreign_keys=[shared_by],
        lazy="joined"
    )
    expires_at = db.Column(db.DateTime, nullable=True)


# ======================
# Notifications
# ======================
class Notification(db.Model):
    # no __tablename__ => default is "notification"
    __table_args__ = (
        db.Index("ix_notification_user_read", "user_id", "is_read"),
        db.Index("ix_notification_created", "created_at"),
        db.Index("ix_notification_event_key", "event_key"),
        db.Index("ix_notification_user_mirror_read", "user_id", "is_mirror", "is_read"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    message = db.Column(db.String(255))
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    type = db.Column(db.String(50), default="INFO")
    role = db.Column(db.String(50), nullable=True)

    # ===== Read-receipts / tracking =====
    # event_key groups notifications that belong to the same emitted event.
    # A mirror notification (is_mirror=True) can be created for the actor (sender)
    # and will be auto-marked as read once all recipients have read their copies.
    event_key = db.Column(db.String(64), nullable=True)
    actor_id = db.Column(db.Integer, nullable=True)
    is_mirror = db.Column(db.Boolean, default=False, nullable=False)


# ======================
# Internal Messaging
# ======================
class Message(db.Model):
    __tablename__ = "messages"

    __table_args__ = (
        db.Index("ix_messages_created", "created_at"),
        db.Index("ix_messages_sender", "sender_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    subject = db.Column(db.String(200), nullable=True)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # for traceability (who was the target user/department/directorate)
    target_kind = db.Column(db.String(20), nullable=False)  # USER / DEPARTMENT / DIRECTORATE
    target_id = db.Column(db.Integer, nullable=False)

    # Reply/thread support (optional)
    reply_to_id = db.Column(db.Integer, nullable=True)  # message_id being replied to

    # Soft delete flags
    sender_deleted = db.Column(db.Boolean, default=False, nullable=False)
    sender_deleted_at = db.Column(db.DateTime, nullable=True)

    sender = db.relationship("User", lazy="joined")


class MessageRecipient(db.Model):
    __tablename__ = "message_recipients"

    __table_args__ = (
        db.Index("ix_msgrec_user_read", "recipient_user_id", "is_read"),
        db.Index("ix_msgrec_message", "message_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey("messages.id"), nullable=False)
    recipient_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    is_read = db.Column(db.Boolean, default=False, nullable=False)
    read_at = db.Column(db.DateTime, nullable=True)

    # Soft delete per-recipient
    is_deleted = db.Column(db.Boolean, default=False, nullable=False)
    deleted_at = db.Column(db.DateTime, nullable=True)

    message = db.relationship("Message", backref=db.backref("recipients", lazy="selectin"))
    recipient = db.relationship("User", lazy="joined")


# ======================
# Workflow Path (Templates + Instances)
# ======================
class WorkflowTemplate(db.Model):
    __tablename__ = "workflow_templates"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    is_active = db.Column(db.Boolean, default=True)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # SLA default for this template (optional)
    sla_days_default = db.Column(db.Integer, nullable=True)

    steps = db.relationship(
        "WorkflowTemplateStep",
        backref="template",
        cascade="all, delete-orphan",
        order_by="WorkflowTemplateStep.step_order"
    )


class WorkflowTemplateStep(db.Model):
    __tablename__ = "workflow_template_steps"

    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(db.Integer, db.ForeignKey("workflow_templates.id"), nullable=False)

    step_order = db.Column(db.Integer, nullable=False)  # 1..n

    # Approver target: USER / DEPARTMENT / ROLE
    approver_kind = db.Column(db.String(20), nullable=False)

    approver_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    approver_department_id = db.Column(db.Integer, nullable=True)  # no departments table in your schema
    approver_role = db.Column(db.String(50), nullable=True)

    # SLA override for this step (optional)
    sla_days = db.Column(db.Integer, nullable=True)


class WorkflowInstance(db.Model):
    __tablename__ = "workflow_instances"

    id = db.Column(db.Integer, primary_key=True)

    request_id = db.Column(
        db.Integer,
        db.ForeignKey("workflow_request.id"),
        nullable=False,
        unique=True
    )

    template_id = db.Column(db.Integer, db.ForeignKey("workflow_templates.id"), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    current_step_order = db.Column(db.Integer, default=1)
    is_completed = db.Column(db.Boolean, default=False)

    steps = db.relationship(
        "WorkflowInstanceStep",
        backref="instance",
        cascade="all, delete-orphan",
        order_by="WorkflowInstanceStep.step_order"
    )


class WorkflowInstanceStep(db.Model):
    __tablename__ = "workflow_instance_steps"

    id = db.Column(db.Integer, primary_key=True)
    instance_id = db.Column(db.Integer, db.ForeignKey("workflow_instances.id"), nullable=False)

    step_order = db.Column(db.Integer, nullable=False)

    approver_kind = db.Column(db.String(20), nullable=False)
    approver_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    approver_department_id = db.Column(db.Integer, nullable=True)
    approver_role = db.Column(db.String(50), nullable=True)

    status = db.Column(db.String(30), default="PENDING")  # PENDING / APPROVED / REJECTED / SKIPPED

    decided_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    decided_at = db.Column(db.DateTime, nullable=True)
    note = db.Column(db.Text, nullable=True)

    # SLA
    due_at = db.Column(db.DateTime, nullable=True)


# ======================
# Attachments: link workflow requests to archived files
# ======================
class RequestAttachment(db.Model):
    __tablename__ = "workflow_request_attachments"

    id = db.Column(db.Integer, primary_key=True)

    # ✅ WorkflowRequest table is "workflow_request"
    request_id = db.Column(db.Integer, db.ForeignKey("workflow_request.id"), nullable=False)

    # ✅ ArchivedFile table is "archived_file"
    archived_file_id = db.Column(db.Integer, db.ForeignKey("archived_file.id"), nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    archived_file = db.relationship("ArchivedFile", backref="workflow_attachments")


# ======================
# Role permissions
# ======================
class RolePermission(db.Model):
    __tablename__ = "role_permission"

    id = db.Column(db.Integer, primary_key=True)
    role = db.Column(db.String(50), nullable=False)
    permission = db.Column(db.String(100), nullable=False)

    __table_args__ = (
        db.UniqueConstraint("role", "permission", name="uix_role_permission"),
    )


class Organization(db.Model):
    __tablename__ = "organizations"
    id = db.Column(db.Integer, primary_key=True)
    name_ar = db.Column(db.String(200), nullable=False)
    name_en = db.Column(db.String(200), nullable=True)
    code = db.Column(db.String(50), unique=True, nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

class Directorate(db.Model):
    __tablename__ = "directorates"
    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id"), nullable=False)
    name_ar = db.Column(db.String(200), nullable=False)
    name_en = db.Column(db.String(200), nullable=True)
    code = db.Column(db.String(50), nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    organization = db.relationship("Organization", backref=db.backref("directorates", lazy="selectin"))

class Department(db.Model):
    __tablename__ = "departments"
    id = db.Column(db.Integer, primary_key=True)
    directorate_id = db.Column(db.Integer, db.ForeignKey("directorates.id"), nullable=False)
    name_ar = db.Column(db.String(200), nullable=False)
    name_en = db.Column(db.String(200), nullable=True)
    code = db.Column(db.String(50), nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    directorate = db.relationship("Directorate", backref=db.backref("departments", lazy="selectin"))

# ======================
# Request Types + Routing Rules
# ======================
class RequestType(db.Model):
    __tablename__ = "request_types"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False, index=True)
    name_ar = db.Column(db.String(200), nullable=False)
    name_en = db.Column(db.String(200), nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<RequestType {self.code}>"


class WorkflowRoutingRule(db.Model):
    __tablename__ = "workflow_routing_rules"

    id = db.Column(db.Integer, primary_key=True)

    request_type_id = db.Column(db.Integer, db.ForeignKey("request_types.id"), nullable=False, index=True)

    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id"), nullable=True, index=True)
    directorate_id = db.Column(db.Integer, db.ForeignKey("directorates.id"), nullable=True, index=True)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=True, index=True)

    template_id = db.Column(db.Integer, db.ForeignKey("workflow_templates.id"), nullable=False, index=True)

    priority = db.Column(db.Integer, default=100, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    request_type = db.relationship("RequestType", lazy="joined")
    template = db.relationship("WorkflowTemplate", lazy="joined")

    organization = db.relationship("Organization", lazy="joined")
    directorate = db.relationship("Directorate", lazy="joined")
    department = db.relationship("Department", lazy="joined")

    __table_args__ = (
        db.Index(
            "ix_routing_rule_match",
            "request_type_id",
            "organization_id",
            "directorate_id",
            "department_id",
            "is_active"
        ),
    )

    def specificity_score(self) -> int:
        score = 0
        if self.organization_id is not None:
            score += 1
        if self.directorate_id is not None:
            score += 2
        if self.department_id is not None:
            score += 3
        return score


class UserPermission(db.Model):
    __tablename__ = "user_permissions"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    key = db.Column(db.String(100), nullable=False, index=True)
    is_allowed = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("user_id", "key", name="uq_user_permissions_user_key"),
    )