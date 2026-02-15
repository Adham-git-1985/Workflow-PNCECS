from datetime import datetime, timedelta
import json
import unicodedata

from extensions import db
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from sqlalchemy import func


# ======================
# Roles (Master Data)
# ======================
class Role(db.Model):
    __tablename__ = "roles"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, index=True, nullable=False)
    name_ar = db.Column(db.String(200), nullable=True)
    name_en = db.Column(db.String(200), nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    @property
    def label(self):
        return (self.name_ar or self.name_en or self.code or "").strip() or self.code



# ======================
# Committees (Master Data)
# ======================
class Committee(db.Model):
    __tablename__ = "committees"

    id = db.Column(db.Integer, primary_key=True)
    name_ar = db.Column(db.String(255), nullable=False)
    name_en = db.Column(db.String(255), nullable=True)
    code = db.Column(db.String(50), nullable=True, index=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    assignees = db.relationship(
        "CommitteeAssignee",
        back_populates="committee",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="CommitteeAssignee.id",
    )

    @property
    def label(self):
        return (self.name_ar or self.name_en or self.code or f"Committee #{self.id}")


class CommitteeAssignee(db.Model):
    __tablename__ = "committee_assignees"

    id = db.Column(db.Integer, primary_key=True)

    committee_id = db.Column(db.Integer, db.ForeignKey("committees.id"), nullable=False, index=True)

    # USER or ROLE
    kind = db.Column(db.String(20), nullable=False)

    # When kind=USER
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    # When kind=ROLE
    role = db.Column(db.String(50), nullable=True, index=True)

    # CHAIR / SECRETARY / MEMBER (optional)
    member_role = db.Column(db.String(20), nullable=True)

    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    committee = db.relationship("Committee", back_populates="assignees")
    user = db.relationship("User", foreign_keys=[user_id], lazy="joined")

    __table_args__ = (
        db.CheckConstraint("kind IN ('USER','ROLE')", name="ck_committee_assignee_kind"),
    )


# ======================
# Users
# ======================
class User(db.Model, UserMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, index=True)
    name = db.Column(db.String(200), nullable=True)
    job_title = db.Column(db.String(200), nullable=True)
    avatar_filename = db.Column(db.String(255), nullable=True)  # profile photo
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(50), index=True)
    department_id = db.Column(db.Integer, nullable=True)
    # Optional explicit directorate assignment (useful for directorate heads)
    # If NULL, directorate is derived from department_id.
    directorate_id = db.Column(db.Integer, nullable=True, index=True)
    # Optional explicit Unit/Section/Division assignment (for richer org structure)
    unit_id = db.Column(db.Integer, db.ForeignKey("units.id"), nullable=True, index=True)
    section_id = db.Column(db.Integer, db.ForeignKey("sections.id"), nullable=True, index=True)
    division_id = db.Column(db.Integer, db.ForeignKey("divisions.id"), nullable=True, index=True)
    org_node_id = db.Column(db.Integer, db.ForeignKey("org_nodes.id"), nullable=True, index=True)

    # Last successful login tracking (Portal HR report)
    last_login_success_at = db.Column(db.DateTime, nullable=True, index=True)
    last_login_success_ip = db.Column(db.String(64), nullable=True)
    last_login_success_ua = db.Column(db.String(255), nullable=True)
    permissions = db.relationship(
        "UserPermission",
        backref="user",
        lazy="selectin",
        cascade="all, delete-orphan"
    )

    # Portal HR: EmployeeFile has (user_id) + (updated_by_id) FKs to users.
    # Defining this explicitly avoids AmbiguousForeignKeysError for the reverse join.
    employee_file = db.relationship(
        "EmployeeFile",
        uselist=False,
        foreign_keys="EmployeeFile.user_id",
        back_populates="user",
        lazy="selectin",
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

        key = (key or "").strip().upper()

        # IMPORTANT: Delegation must NOT reduce the privileges of the real logged-in account.
        # We evaluate the current user's own permissions first, then (optionally) OR with the
        # effective (delegator) user's permissions.
        eff = None
        try:
            from flask import has_request_context, g  # type: ignore
            if has_request_context():
                eff = getattr(g, "effective_user", None)
        except Exception:
            eff = None

        # SUPER/ADMIN can do everything (robust against naming variations), even if delegation is active.
        try:
            role_raw = (getattr(self, "role", "") or "").strip().upper().replace("-", "_").replace(" ", "_")
            role_raw = unicodedata.normalize("NFKC", role_raw)
            role_raw = "".join(ch for ch in role_raw if (ch.isalnum() or ch == "_"))
            if role_raw.startswith("SUPER"):
                return True
            if role_raw in ("ADMIN",):
                return True
        except Exception:
            pass

        if not key:
            return False

        perms = [
            (p.key or "").strip().upper()
            for p in (self.permissions or [])
            if getattr(p, "is_allowed", False)
        ]

        # Merge ROLE permissions (RolePermission) so role-based permissions are honored
        # This keeps template checks (current_user.has_perm) and perm_required consistent with /admin/permissions.
        try:
            from sqlalchemy import func
            role_raw = (getattr(self, "role", "") or "").strip()
            role = role_raw.lower()
            if role:
                role_rows = (
                    RolePermission.query
                    .filter(func.lower(RolePermission.role) == role)
                    .all()
                )

                # If role-perms not found, try resolving role string via Role masterdata
                # (helps when users store role as Arabic/English name instead of code).
                if not role_rows:
                    try:
                        _r = (
                            Role.query
                            .filter(func.lower(Role.code) == role)
                            .first()
                        )
                        if not _r and role_raw:
                            _r = (
                                Role.query
                                .filter(func.lower(Role.name_en) == role)
                                .first()
                            )
                        if not _r and role_raw:
                            _r = Role.query.filter(Role.name_ar == role_raw).first()
                        if _r and (_r.code or "").strip():
                            role2 = (_r.code or "").strip().lower()
                            if role2 and role2 != role:
                                role_rows = (
                                    RolePermission.query
                                    .filter(func.lower(RolePermission.role) == role2)
                                    .all()
                                )
                    except Exception:
                        pass
                role_perms = [
                    (rp.permission or "").strip().upper()
                    for rp in role_rows
                ]
                if role_perms:
                    perms = list(set(perms + role_perms))
        except Exception:
            pass
        # Backward-compatibility aliases for Portal permissions (old keys -> new CRUD-like keys)
        try:
            from portal.perm_defs import ALIASES as _PORTAL_ALIASES
            _extra = []
            for _k in list(perms):
                _alias = _PORTAL_ALIASES.get(_k)
                if _alias and _alias not in perms:
                    _extra.append(_alias)
            if _extra:
                perms = list(set(perms + _extra))
        except Exception:
            pass
        # Implicit READ: إذا امتلك المستخدم CREATE/UPDATE/DELETE/EXPORT لموديول ما، اعتبر READ متاحًا أيضًا
        try:
            _derived = []
            for _k in list(perms):
                for _suffix in ("_CREATE", "_UPDATE", "_DELETE", "_EXPORT"):
                    if _k.endswith(_suffix):
                        _base = _k[: -len(_suffix)]
                        _read = f"{_base}_READ"
                        if _read not in perms:
                            _derived.append(_read)
            if _derived:
                perms = list(set(perms + _derived))
        except Exception:
            pass

        def _eval(perms_list: list[str]) -> bool:
            if key in perms_list:
                return True
            actions = ("READ", "CREATE", "UPDATE", "DELETE")
            for act in actions:
                suffix = "_" + act
                if key.endswith(suffix):
                    base = key[: -len(suffix)]
                    return f"{base}_MANAGE" in perms_list
            if key.endswith("_MANAGE"):
                base = key[: -len("_MANAGE")]
                return all(f"{base}_{act}" in perms_list for act in actions)
            return False

        # Evaluate self permissions first
        if _eval(perms):
            return True

        # If delegation is active, OR with effective user's permissions.
        try:
            if eff is not None and getattr(eff, "id", None) is not None and eff.id != self.id:
                return bool(eff.has_perm(key))
        except Exception:
            pass
        return False

    def has_role(self, role_name):
        """Return True if the user has the given role.

        Robustness:
        - Some deployments store User.role as Role.code (e.g., SUPER_ADMIN)
        - Others may store the Arabic/English display name (Role.name_ar / Role.name_en)
        This method resolves to the Role.code when possible.

        Compatibility notes:
        - SUPERADMIN and SUPER_ADMIN are treated the same.
        - SUPERADMIN inherits ADMIN privileges.
        """

        # IMPORTANT: Delegation must NOT reduce the privileges of the real logged-in account.
        # We evaluate self role first, then (optionally) OR with the effective (delegator) role.
        eff = None
        try:
            from flask import has_request_context, g  # type: ignore
            if has_request_context():
                eff = getattr(g, "effective_user", None)
        except Exception:
            eff = None

        def _norm(x: str) -> str:
            """Normalize role/code text very defensively.

            We occasionally see invisible unicode formatting marks (RLM/LRM/ZWJ, etc.)
            coming from copy/paste or RTL UIs. Those break simple string comparisons.
            This function strips those marks and keeps only [A-Z0-9_].
            """
            s = (x or "").strip().upper().replace("-", "_").replace(" ", "_")
            try:
                s = unicodedata.normalize("NFKC", s)
                s = "".join(ch for ch in s if (ch.isalnum() or ch == "_"))
            except Exception:
                pass
            return s

        want = _norm(role_name)
        if not want:
            return False

        raw = (self.role or "").strip()
        if not raw:
            return False

        mine = _norm(raw)

        # Try to resolve stored role label -> Role.code
        try:
            # 1) match by code (case-insensitive)
            r = Role.query.filter(func.upper(Role.code) == mine).first()
            if r:
                mine = _norm(r.code)
            else:
                # 2) match by Arabic/English display name
                raw_lower = raw.lower()
                r = Role.query.filter(
                    (Role.name_ar == raw) | (func.lower(Role.name_en) == raw_lower)
                ).first()
                if r:
                    mine = _norm(r.code)
        except Exception:
            pass

        # Heuristic fallback for Arabic/loose labels (avoids blocking SUPER_ADMIN by label variations)
        try:
            raw_clean = (raw or '').strip()
            raw_lower = raw_clean.lower()
            if (('سوبر' in raw_clean) and ('أدمن' in raw_clean or 'ادمن' in raw_clean)) or (('super' in raw_lower) and ('admin' in raw_lower)):
                mine = 'SUPER_ADMIN'
        except Exception:
            pass

        # Extended heuristic fallback for super/system admin labels
        try:
            raw_clean = (raw or '').strip()
            raw_lower = raw_clean.lower()

            # If the normalized role already contains SUPER+ADMIN (e.g., SUPER_ADMINISTRATOR), treat as SUPER_ADMIN
            if ('SUPER' in mine and 'ADMIN' in mine) or ('SYSTEM' in mine and 'ADMIN' in mine):
                mine = 'SUPER_ADMIN'

            # Common English aliases
            if raw_lower in ('root', 'superuser', 'sysadmin', 'systemadmin', 'system_admin', 'administrator', 'admin_root'):
                mine = 'SUPER_ADMIN'

            # Common Arabic aliases
            if ('مدير' in raw_clean and 'نظام' in raw_clean) and (('أعلى' in raw_clean) or ('اعلى' in raw_clean) or ('عليا' in raw_clean) or ('الاعلى' in raw_clean) or ('الأعلى' in raw_clean)):
                mine = 'SUPER_ADMIN'

            # Ultimate safe fallback: first user (id=1) is treated as SUPER_ADMIN
            if getattr(self, 'id', None) == 1:
                mine = 'SUPER_ADMIN'
        except Exception:
            pass

        # SUPERADMIN synonyms
        if mine in ("SUPERADMIN", "SUPER_ADMIN") and want in ("SUPERADMIN", "SUPER_ADMIN"):
            return True

        # SUPERADMIN inherits ADMIN
        if mine in ("SUPERADMIN", "SUPER_ADMIN") and want == "ADMIN":
            return True

        # Direct match
        if mine == want:
            return True

        # If delegation is active, OR with effective user's role.
        try:
            if eff is not None and getattr(eff, "id", None) is not None and eff.id != self.id:
                return bool(eff.has_role(role_name))
        except Exception:
            pass
        return False

    def has_role_perm(self, permission: str) -> bool:
        """Role-based permissions (RolePermission table).

        - ADMIN (and SUPER_ADMIN via has_role('ADMIN')) always allowed.
        - Otherwise: checks RolePermission where role matches current user's role (case-insensitive)
          and permission matches (case-insensitive stored as upper).
        """
        perm = (permission or "").strip().upper()
        # SUPERADMIN can do everything
        if self.has_role("SUPERADMIN") or self.has_role("SUPER_ADMIN"):
            return True

        # ADMIN: system-wide (highest) access.
        if self.has_role("ADMIN"):
            return True

        if not perm:
            return False

        raw_role = (self.role or "").strip()
        if not raw_role:
            return False

        # Resolve stored role label -> Role.code when possible
        role_norm = None
        try:
            def _norm(x: str) -> str:
                return (x or "").strip().upper().replace("-", "_").replace(" ", "_")

            mine = _norm(raw_role)
            r = Role.query.filter(func.upper(Role.code) == mine).first()
            if r:
                role_norm = (r.code or raw_role).strip().lower()
            else:
                r = Role.query.filter((Role.name_ar == raw_role) | (func.lower(Role.name_en) == raw_role.lower())).first()
                if r:
                    role_norm = (r.code or raw_role).strip().lower()
        except Exception:
            role_norm = None

        if not role_norm:
            role_norm = raw_role.lower()

        return (
            RolePermission.query
            .filter(func.lower(RolePermission.role) == role_norm)
            .filter(RolePermission.permission == perm)
            .first()
            is not None
        )


    @property
    def full_name(self):
        return (self.name or "").strip() or self.email or f"User #{self.id}"

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

# ======================
# Delegations (تفويض الصلاحيات)
# ======================
class Delegation(db.Model):
    __tablename__ = "delegations"

    __table_args__ = (
        db.Index("ix_delegations_to_active_window", "to_user_id", "is_active", "starts_at", "expires_at"),
        db.Index("ix_delegations_from_active_window", "from_user_id", "is_active", "starts_at", "expires_at"),
    )

    id = db.Column(db.Integer, primary_key=True)

    # Delegator (الأصيل) -> Delegatee (المفوّض إليه)
    from_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    to_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    starts_at = db.Column(db.DateTime, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)

    # "is_active" هنا تعني: لم يتم إلغاء التفويض (حتى لو انتهت المدة)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    note = db.Column(db.Text, nullable=True)

    from_user = db.relationship("User", foreign_keys=[from_user_id], lazy="joined")
    to_user = db.relationship("User", foreign_keys=[to_user_id], lazy="joined")
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
    @property
    def is_effective_now(self) -> bool:
        if not self.is_active:
            return False
        now = datetime.now()
        return self.starts_at <= now <= self.expires_at

    def __repr__(self) -> str:
        return f"<Delegation id={self.id} from={self.from_user_id} to={self.to_user_id} active={self.is_active}>"

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

    # Delegation context (when an action is performed by a delegatee on behalf of another user)
    on_behalf_of_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    delegation_id = db.Column(db.Integer, db.ForeignKey("delegations.id"), nullable=True)

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

    user = db.relationship("User", foreign_keys=[user_id])
    on_behalf_of_user = db.relationship("User", foreign_keys=[on_behalf_of_id], lazy="joined")
    delegation = db.relationship("Delegation", foreign_keys=[delegation_id], lazy="joined")
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
# Portal Permission Presets (shortcuts)
# ======================
class PortalPermissionPreset(db.Model):
    __tablename__ = "portal_permission_preset"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    code = db.Column(db.String(50), unique=True, nullable=False)          # e.g. EMPLOYEE
    label = db.Column(db.String(120), nullable=False)                      # Arabic label
    category = db.Column(db.String(20), default="extra")                  # main / extra
    keys_json = db.Column(db.Text, nullable=False, default="[]")          # JSON list of permission keys
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


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

    # Final deletion: hidden from everyone except SUPER_ADMIN (Super Trash)
    is_final_deleted = db.Column(db.Boolean, default=False)
    final_deleted_at = db.Column(db.DateTime)
    final_deleted_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    final_deleted_by_user = db.relationship(
        "User",
        foreign_keys=[final_deleted_by],
        backref="final_deleted_archived_files",
        lazy="joined"
    )

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
        db.Index("ix_notification_user_source_read", "user_id", "source", "is_read"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    message = db.Column(db.String(255))
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    type = db.Column(db.String(50), default="INFO")
    role = db.Column(db.String(50), nullable=True)

    # source of notification: 'workflow' or 'portal' (helps UI separation)
    source = db.Column(db.String(20), default="workflow", nullable=True)

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
class RequestEscalation(db.Model):
    __tablename__ = "request_escalation"

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("workflow_request.id"), nullable=False)

    from_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    to_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    category = db.Column(db.String(50), nullable=False)
    description = db.Column(db.Text, nullable=False)

    # Which workflow step was escalated (best-effort)
    step_order = db.Column(db.Integer, nullable=True)

    # Store recipients/CC for traceability (comma-separated user ids or emails)
    targets = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    request = db.relationship("WorkflowRequest", backref="escalations")
    from_user = db.relationship("User", foreign_keys=[from_user_id], lazy="joined")
    to_user = db.relationship("User", foreign_keys=[to_user_id], lazy="joined")

    def __repr__(self):
        return f"<RequestEscalation #{self.id} req={self.request_id} step={getattr(self,'step_order',None)} {self.category}>"



class WorkflowTemplateStep(db.Model):
    __tablename__ = "workflow_template_steps"

    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(db.Integer, db.ForeignKey("workflow_templates.id"), nullable=False)

    step_order = db.Column(db.Integer, nullable=False)  # 1..n

    # Execution mode:
    # - SEQUENTIAL: normal approval decision drives routing.
    # - PARALLEL_SYNC: distribute to multiple assignees at the same time;
    #   routing decision is NOT affected by approve/reject (responses are for documentation).
    mode = db.Column(db.String(20), nullable=False, default="SEQUENTIAL")

    # Approver target: USER / DEPARTMENT / ROLE
    approver_kind = db.Column(db.String(20), nullable=False)

    approver_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    approver_department_id = db.Column(db.Integer, nullable=True)
    approver_directorate_id = db.Column(db.Integer, nullable=True)  # no departments table in your schema
    # Extra org structure targets
    approver_unit_id = db.Column(db.Integer, db.ForeignKey("units.id"), nullable=True)
    approver_section_id = db.Column(db.Integer, db.ForeignKey("sections.id"), nullable=True)
    approver_division_id = db.Column(db.Integer, db.ForeignKey("divisions.id"), nullable=True)
    approver_org_node_id = db.Column(db.Integer, db.ForeignKey("org_nodes.id"), nullable=True)


    approver_role = db.Column(db.String(50), nullable=True)

    # Committee target
    approver_committee_id = db.Column(db.Integer, db.ForeignKey('committees.id'), nullable=True)
    committee_delivery_mode = db.Column(db.String(30), nullable=True)  # Committee_ALL / Committee_CHAIR / Committee_SECRETARY

    committee = db.relationship('Committee', foreign_keys=[approver_committee_id], lazy='joined')

    # SLA override for this step (optional)
    sla_days = db.Column(db.Integer, nullable=True)


# Extra assignees for PARALLEL_SYNC steps (linked to step number)
class WorkflowTemplateParallelAssignee(db.Model):
    __tablename__ = 'workflow_template_parallel_assignees'

    id = db.Column(db.Integer, primary_key=True)

    template_step_id = db.Column(db.Integer, db.ForeignKey('workflow_template_steps.id'), nullable=False, index=True)

    # Denormalized for readability/reporting; source of truth is template_step_id
    template_id = db.Column(db.Integer, nullable=False, index=True)
    step_order = db.Column(db.Integer, nullable=False, index=True)

    approver_kind = db.Column(db.String(20), nullable=False)  # USER/ROLE/DEPARTMENT/DIRECTORATE/COMMITTEE

    approver_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    approver_department_id = db.Column(db.Integer, nullable=True)
    approver_directorate_id = db.Column(db.Integer, nullable=True)
    # Extra org structure targets
    approver_unit_id = db.Column(db.Integer, db.ForeignKey("units.id"), nullable=True)
    approver_section_id = db.Column(db.Integer, db.ForeignKey("sections.id"), nullable=True)
    approver_division_id = db.Column(db.Integer, db.ForeignKey("divisions.id"), nullable=True)
    approver_org_node_id = db.Column(db.Integer, db.ForeignKey("org_nodes.id"), nullable=True)


    approver_role = db.Column(db.String(50), nullable=True)

    # Committee target
    approver_committee_id = db.Column(db.Integer, db.ForeignKey('committees.id'), nullable=True)
    committee_delivery_mode = db.Column(db.String(30), nullable=True)  # Committee_ALL / Committee_CHAIR / Committee_SECRETARY

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship('User', foreign_keys=[approver_user_id], lazy='joined')

    __table_args__ = (
        db.Index('ix_wf_par_assignee_step', 'template_id', 'step_order'),
    )


# Add relationship on WorkflowTemplateStep (declare after class exists)
WorkflowTemplateStep.parallel_assignees = db.relationship(
    'WorkflowTemplateParallelAssignee',
    primaryjoin='WorkflowTemplateStep.id==WorkflowTemplateParallelAssignee.template_step_id',
    cascade='all, delete-orphan',
    lazy='selectin',
    order_by='WorkflowTemplateParallelAssignee.id',
)


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

    # For PARALLEL_SYNC bypass authority: who executed the previous step (effective user id)
    last_step_actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    last_step_actor = db.relationship("User", foreign_keys=[last_step_actor_id], lazy="joined")

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

    mode = db.Column(db.String(20), nullable=False, default="SEQUENTIAL")

    approver_kind = db.Column(db.String(20), nullable=False)
    approver_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    approver_department_id = db.Column(db.Integer, nullable=True)
    approver_directorate_id = db.Column(db.Integer, nullable=True)
    # Extra org structure targets
    approver_unit_id = db.Column(db.Integer, db.ForeignKey("units.id"), nullable=True)
    approver_section_id = db.Column(db.Integer, db.ForeignKey("sections.id"), nullable=True)
    approver_division_id = db.Column(db.Integer, db.ForeignKey("divisions.id"), nullable=True)
    approver_org_node_id = db.Column(db.Integer, db.ForeignKey("org_nodes.id"), nullable=True)


    approver_role = db.Column(db.String(50), nullable=True)

    # Committee target
    approver_committee_id = db.Column(db.Integer, db.ForeignKey('committees.id'), nullable=True)
    committee_delivery_mode = db.Column(db.String(30), nullable=True)  # Committee_ALL / Committee_CHAIR / Committee_SECRETARY

    status = db.Column(db.String(30), default="PENDING")  # PENDING / APPROVED / REJECTED / SKIPPED

    decided_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    decided_at = db.Column(db.DateTime, nullable=True)
    note = db.Column(db.Text, nullable=True)

    # SLA
    due_at = db.Column(db.DateTime, nullable=True)

    # PARALLEL_SYNC: ensure notifications are sent once when the step becomes active
    parallel_notified_at = db.Column(db.DateTime, nullable=True)


class WorkflowStepTask(db.Model):
    """Runtime tasks for PARALLEL_SYNC steps.

    A PARALLEL_SYNC step creates one task per assignee.
    The workflow moves to the next step only when all tasks are RESPONDED or BYPASSED.
    """

    __tablename__ = "workflow_step_tasks"

    __table_args__ = (
        db.UniqueConstraint("instance_id", "step_order", "assignee_user_id", name="uq_wf_parallel_task"),
    )

    id = db.Column(db.Integer, primary_key=True)

    instance_id = db.Column(db.Integer, db.ForeignKey("workflow_instances.id"), nullable=False, index=True)
    request_id = db.Column(db.Integer, db.ForeignKey("workflow_request.id"), nullable=False, index=True)
    step_order = db.Column(db.Integer, nullable=False, index=True)

    assignee_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    status = db.Column(db.String(20), nullable=False, default="PENDING")  # PENDING/RESPONDED/BYPASSED
    response = db.Column(db.String(20), nullable=False, default="NONE")  # APPROVE/REJECT/NONE
    note = db.Column(db.Text, nullable=True)

    responded_at = db.Column(db.DateTime, nullable=True)

    bypassed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    bypass_reason = db.Column(db.String(500), nullable=True)
    bypassed_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    assignee = db.relationship("User", foreign_keys=[assignee_user_id], lazy="joined")
    bypassed_by = db.relationship("User", foreign_keys=[bypassed_by_id], lazy="joined")
    instance = db.relationship(
        "WorkflowInstance",
        foreign_keys=[instance_id],
        backref=db.backref("step_tasks", lazy="selectin", cascade="all, delete-orphan"),
    )

    __table_args__ = (
        db.UniqueConstraint("instance_id", "step_order", "assignee_user_id", name="uq_workflow_step_task"),
    )

    # (No SLA fields yet for PARALLEL_SYNC tasks)


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



# ======================
# User permissions (per-user portal/service permissions)
# ======================
class UserPermission(db.Model):
    """Per-user permission keys.

    Used by the Portal and Admin Permissions screens.
    """

    __tablename__ = "user_permission"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    # Permission key (stored uppercased, e.g. PORTAL_READ, CORR_CREATE)
    key = db.Column(db.String(120), nullable=False, index=True)

    # Soft toggle (keep row, allow deny)
    is_allowed = db.Column(db.Boolean, default=True, nullable=False, index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("user_id", "key", name="uq_user_permission_user_key"),
        db.Index("ix_user_permission_user_allowed", "user_id", "is_allowed"),
        db.Index("ix_user_permission_key_allowed", "key", "is_allowed"),
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


class Unit(db.Model):
    __tablename__ = "units"

    id = db.Column(db.Integer, primary_key=True)

    # Units are top-level under Organization (not under Directorate)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id"), nullable=True, index=True)

    name_ar = db.Column(db.String(200), nullable=False)
    name_en = db.Column(db.String(200), nullable=True)
    code = db.Column(db.String(50), nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    organization = db.relationship("Organization", backref=db.backref("units", lazy="selectin"))

    def __repr__(self) -> str:
        return f"<Unit {self.name_ar}>"

    @property
    def label(self) -> str:
        return (self.name_ar or self.name_en or self.code or '').strip() or str(self.id)


class Department(db.Model):
    __tablename__ = "departments"
    __table_args__ = (
        db.CheckConstraint(
            "(directorate_id IS NOT NULL AND unit_id IS NULL) OR (directorate_id IS NULL AND unit_id IS NOT NULL)",
            name="ck_departments_parent_xor",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)

    # Department can belong either to a Directorate OR to a Unit (must choose one)
    directorate_id = db.Column(db.Integer, db.ForeignKey("directorates.id"), nullable=True, index=True)
    unit_id = db.Column(db.Integer, db.ForeignKey("units.id"), nullable=True, index=True)

    name_ar = db.Column(db.String(200), nullable=False)
    name_en = db.Column(db.String(200), nullable=True)
    code = db.Column(db.String(50), nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    directorate = db.relationship("Directorate", backref=db.backref("departments", lazy="selectin"))
    unit = db.relationship("Unit", backref=db.backref("departments", lazy="selectin"))

    @property
    def effective_directorate_id(self):
        # Department may be attached to a Directorate OR to a Unit.
        # If attached to a Unit, there is no directorate in the hierarchy (unit is under organization).
        return int(self.directorate_id) if self.directorate_id else None

    def __repr__(self) -> str:
        return f"<Department {self.name_ar}>"


# ======================
# Request Types + Workflow Routing Rules
# ======================
class RequestType(db.Model):
    __tablename__ = "request_types"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, index=True, nullable=False)
    name_ar = db.Column(db.String(200), nullable=False)
    name_en = db.Column(db.String(200), nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    @property
    def label(self) -> str:
        return (self.name_ar or self.name_en or self.code or "").strip() or self.code

    def __repr__(self) -> str:
        return f"<RequestType {self.code}>"


class WorkflowRoutingRule(db.Model):
    __tablename__ = "workflow_routing_rules"

    id = db.Column(db.Integer, primary_key=True)

    request_type_id = db.Column(db.Integer, db.ForeignKey("request_types.id"), nullable=False, index=True)

    # Optional hierarchy match (NULL means 'any')
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id"), nullable=True, index=True)
    directorate_id = db.Column(db.Integer, db.ForeignKey("directorates.id"), nullable=True, index=True)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=True, index=True)

    # Optional dynamic OrgNode scope (overrides or complements fixed hierarchy).
    org_node_id = db.Column(db.Integer, db.ForeignKey("org_nodes.id"), nullable=True, index=True)
    match_subtree = db.Column(db.Boolean, default=True, nullable=False)

    template_id = db.Column(db.Integer, db.ForeignKey("workflow_templates.id"), nullable=False, index=True)

    priority = db.Column(db.Integer, default=100, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    request_type = db.relationship("RequestType", lazy="joined")
    organization = db.relationship("Organization", lazy="joined")
    directorate = db.relationship("Directorate", lazy="joined")
    department = db.relationship("Department", lazy="joined")
    org_node = db.relationship("OrgNode", lazy="joined")
    template = db.relationship("WorkflowTemplate", lazy="joined")

    __table_args__ = (
        db.Index(
            "ix_routing_rule_match",
            "request_type_id",
            "organization_id",
            "directorate_id",
            "department_id",
            "org_node_id",
            "is_active",
        ),
    )

    def specificity_score(self) -> int:
        score = 0
        # Dynamic node scope is considered the most specific match.
        if self.org_node_id is not None:
            score += 3
        if self.organization_id is not None:
            score += 1
        if self.directorate_id is not None:
            score += 1
        if self.department_id is not None:
            score += 1
        return score

    def __repr__(self) -> str:
        return (
            f"<WorkflowRoutingRule id={self.id} rt={self.request_type_id} "
            f"org={self.organization_id} dir={self.directorate_id} dept={self.department_id} "
            f"node={self.org_node_id} subtree={self.match_subtree} "
            f"tpl={self.template_id} pr={self.priority} active={self.is_active}>"
        )



class Section(db.Model):
    __tablename__ = "sections"
    __table_args__ = (
        db.CheckConstraint(
            "(department_id IS NOT NULL AND directorate_id IS NULL AND unit_id IS NULL) OR "
            "(department_id IS NULL AND directorate_id IS NOT NULL AND unit_id IS NULL) OR "
            "(department_id IS NULL AND directorate_id IS NULL AND unit_id IS NOT NULL)",
            name="ck_sections_parent_xor",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)

    # Section can belong either to a Department OR directly to a Directorate OR directly to a Unit (must choose one)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=True, index=True)
    directorate_id = db.Column(db.Integer, db.ForeignKey("directorates.id"), nullable=True, index=True)
    unit_id = db.Column(db.Integer, db.ForeignKey("units.id"), nullable=True, index=True)

    name_ar = db.Column(db.String(200), nullable=False, index=True)
    name_en = db.Column(db.String(200), nullable=True, index=True)
    code = db.Column(db.String(50), nullable=True, index=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    department = db.relationship("Department", backref=db.backref("sections", lazy="dynamic"))
    directorate = db.relationship("Directorate", backref=db.backref("sections_direct", lazy="dynamic"))
    unit = db.relationship("Unit", backref=db.backref("sections_direct", lazy="dynamic"))

    def __repr__(self):
        return f"<Section {self.name_ar}>"

    def to_dict(self):
        parent_name = None
        parent_type = None
        if self.department_id and self.department:
            parent_type = "DEPARTMENT"
            parent_name = self.department.name_ar
        elif self.unit_id and self.unit:
            parent_type = "UNIT"
            parent_name = self.unit.name_ar
        elif self.directorate_id and self.directorate:
            parent_type = "DIRECTORATE"
            parent_name = self.directorate.name_ar

        return {
            "id": self.id,
            "department_id": self.department_id,
            "directorate_id": self.directorate_id,
            "unit_id": self.unit_id,
            "name_ar": self.name_ar,
            "name_en": self.name_en,
            "code": self.code,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "parent_type": parent_type,
            "parent_name": parent_name,
        }


class Division(db.Model):
    __tablename__ = "divisions"
    __table_args__ = (
        db.CheckConstraint(
            "section_id IS NOT NULL OR department_id IS NOT NULL",
            name="ck_divisions_parent",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)

    # Division can belong either to a Section (قسم) OR directly to a Department (دائرة)
    section_id = db.Column(db.Integer, db.ForeignKey("sections.id"), nullable=True, index=True)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=True, index=True)

    name_ar = db.Column(db.String(200), nullable=False, index=True)
    name_en = db.Column(db.String(200), nullable=True, index=True)
    code = db.Column(db.String(50), nullable=True, index=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    section = db.relationship("Section", backref=db.backref("divisions", lazy="dynamic"))
    department = db.relationship("Department", backref=db.backref("divisions", lazy="dynamic"))

    def __repr__(self):
        return f"<Division {self.name_ar}>"

    def to_dict(self):
        parent_name = None
        parent_type = None
        if self.section_id and self.section:
            parent_type = "SECTION"
            parent_name = self.section.name_ar
        elif self.department_id and self.department:
            parent_type = "DEPARTMENT"
            parent_name = self.department.name_ar

        return {
            "id": self.id,
            "section_id": self.section_id,
            "department_id": self.department_id,
            "name_ar": self.name_ar,
            "name_en": self.name_en,
            "code": self.code,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "parent_type": parent_type,
            "parent_name": parent_name,
        }


class Team(db.Model):
    """Team (فريق) under a Section (قسم). Optional link to Division (شعبة)."""
    __tablename__ = "teams"

    id = db.Column(db.Integer, primary_key=True)
    section_id = db.Column(db.Integer, db.ForeignKey("sections.id"), nullable=False, index=True)
    division_id = db.Column(db.Integer, db.ForeignKey("divisions.id"), nullable=True, index=True)
    name_ar = db.Column(db.String(200), nullable=False)
    name_en = db.Column(db.String(200), nullable=True)
    code = db.Column(db.String(50), nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    section = db.relationship("Section", backref=db.backref("teams", lazy="selectin"))

    division = db.relationship("Division", backref=db.backref("teams", lazy="selectin"))

    @property
    def label(self) -> str:
        return (self.name_ar or self.name_en or "").strip()



class OrgNodeType(db.Model):
    """Dynamic organizational structure level/type.

    Allows administrators to add new hierarchy levels (types) with custom names
    and control where they appear (chart/routes/approvals) + allowed parent types.
    """
    __tablename__ = "org_node_types"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False, index=True)  # stable key, e.g., ORGANIZATION
    name_ar = db.Column(db.String(200), nullable=False, index=True)
    name_en = db.Column(db.String(200), nullable=True, index=True)
    sort_order = db.Column(db.Integer, default=0, nullable=False, index=True)

    allow_in_approvals = db.Column(db.Boolean, default=True, nullable=False)
    show_in_chart = db.Column(db.Boolean, default=True, nullable=False)
    show_in_routes = db.Column(db.Boolean, default=True, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    # JSON list of parent type IDs allowed for nodes of this type. NULL/[] => root-eligible.
    allowed_parent_type_ids_json = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def allowed_parent_type_ids(self) -> list[int]:
        try:
            import json as _json
            if not self.allowed_parent_type_ids_json:
                return []
            v = _json.loads(self.allowed_parent_type_ids_json)
            return [int(x) for x in v if str(x).isdigit()]
        except Exception:
            return []

    def set_allowed_parent_type_ids(self, ids: list[int] | None):
        try:
            import json as _json
            ids = ids or []
            ids2 = []
            for x in ids:
                try:
                    ids2.append(int(x))
                except Exception:
                    continue
            self.allowed_parent_type_ids_json = _json.dumps(sorted(set(ids2)))
        except Exception:
            self.allowed_parent_type_ids_json = None


class OrgNode(db.Model):
    """Dynamic organizational node (instance of OrgNodeType)."""
    __tablename__ = "org_nodes"

    id = db.Column(db.Integer, primary_key=True)

    type_id = db.Column(db.Integer, db.ForeignKey("org_node_types.id"), nullable=False, index=True)
    parent_id = db.Column(db.Integer, db.ForeignKey("org_nodes.id"), nullable=True, index=True)

    name_ar = db.Column(db.String(200), nullable=False, index=True)
    name_en = db.Column(db.String(200), nullable=True, index=True)
    code = db.Column(db.String(50), nullable=True, index=True)

    sort_order = db.Column(db.Integer, nullable=False, default=0)

    is_active = db.Column(db.Boolean, default=True, nullable=False)

    # Optional mapping to legacy fixed tables (Organization/Directorate/...)
    legacy_type = db.Column(db.String(50), nullable=True, index=True)  # e.g., ORGANIZATION, DIRECTORATE, ...
    legacy_id = db.Column(db.Integer, nullable=True, index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    type = db.relationship("OrgNodeType", lazy="joined")
    parent = db.relationship("OrgNode", remote_side=[id], backref=db.backref("children", lazy="select"))

    __table_args__ = (
        db.UniqueConstraint("legacy_type", "legacy_id", name="uq_org_nodes_legacy"),
    )


class OrgNodeManager(db.Model):
    """Manager/deputy assignment for a dynamic OrgNode."""
    __tablename__ = "org_node_managers"

    id = db.Column(db.Integer, primary_key=True)
    node_id = db.Column(db.Integer, db.ForeignKey("org_nodes.id"), nullable=False, unique=True, index=True)

    manager_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    deputy_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    node = db.relationship("OrgNode", lazy="joined")
    manager_user = db.relationship("User", foreign_keys=[manager_user_id], lazy="joined")
    deputy_user = db.relationship("User", foreign_keys=[deputy_user_id], lazy="joined")
    updated_by = db.relationship("User", foreign_keys=[updated_by_id], lazy="joined")


class OrgNodeAssignment(db.Model):
    """Assign a user to a dynamic OrgNode (supports 'primary' assignment)."""
    __tablename__ = "org_node_assignments"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    node_id = db.Column(db.Integer, db.ForeignKey("org_nodes.id"), nullable=False, index=True)

    title = db.Column(db.String(120), nullable=True)
    is_primary = db.Column(db.Boolean, default=False, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    user = db.relationship("User", foreign_keys=[user_id], lazy="joined")
    node = db.relationship("OrgNode", lazy="joined")
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")

    __table_args__ = (
        db.UniqueConstraint("user_id", "node_id", name="uq_org_node_assign_user_node"),
        db.Index("ix_org_node_assign_user_primary", "user_id", "is_primary"),
    )



class OrgUnitManager(db.Model):
    """Direct manager/deputy assignment for org units.

    Keeps assignments separate from masterdata tables.
    """
    __tablename__ = "org_unit_manager"

    id = db.Column(db.Integer, primary_key=True)
    unit_type = db.Column(db.String(20), nullable=False, index=True)  # ORGANIZATION/DIRECTORATE/DEPARTMENT/SECTION/DIVISION/TEAM
    unit_id = db.Column(db.Integer, nullable=False, index=True)

    manager_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    deputy_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    manager_user = db.relationship("User", foreign_keys=[manager_user_id], lazy="joined")
    deputy_user = db.relationship("User", foreign_keys=[deputy_user_id], lazy="joined")
    updated_by = db.relationship("User", foreign_keys=[updated_by_id], lazy="joined")

    __table_args__ = (
        db.UniqueConstraint("unit_type", "unit_id", name="uq_org_unit_manager_type_id"),
    )


class OrgUnitAssignment(db.Model):
    """Portal HR: Assign users to org units (membership), separate from Workflow assignments.

    This is used to:
      - show each employee's direct manager and manager-of-manager chain in the org structure.
      - keep Portal org membership independent from Workflow org membership (User.department_id).

    Notes:
      - Managers/Deputies for units are still stored in OrgUnitManager.
      - Users can have multiple assignments; one may be marked as primary.
    """

    __tablename__ = "org_unit_assignment"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    unit_type = db.Column(db.String(20), nullable=False, index=True)  # ORGANIZATION/DIRECTORATE/DEPARTMENT/SECTION/DIVISION/TEAM
    unit_id = db.Column(db.Integer, nullable=False, index=True)

    # If user has multiple assignments, choose the primary one for reporting/manager chain.
    is_primary = db.Column(db.Boolean, default=False, nullable=False, index=True)

    title = db.Column(db.String(200), nullable=True)  # optional position label

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    user = db.relationship("User", foreign_keys=[user_id], lazy="joined")
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
    __table_args__ = (
        db.UniqueConstraint("user_id", "unit_type", "unit_id", name="uq_org_unit_assignment_user_unit"),
        db.CheckConstraint("unit_type IN ('ORGANIZATION','DIRECTORATE','DEPARTMENT','SECTION','DIVISION','TEAM')", name="ck_org_unit_assignment_type"),
    )



class HRLookupItem(db.Model):
    """Generic lookup item for HR employee file.

    Used for dropdowns that can be managed as CRUD and optionally imported/exported via Excel.
    """
    __tablename__ = "hr_lookup_item"

    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(80), nullable=False, index=True)
    code = db.Column(db.String(80), nullable=False)
    name_ar = db.Column(db.String(255), nullable=False, default="")
    name_en = db.Column(db.String(255), nullable=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('category', 'code', name='uq_hr_lookup_category_code'),
    )

    @property
    def label(self) -> str:
        return (self.name_ar or '').strip() or (self.name_en or '').strip() or (self.code or '').strip()


class EmployeeFile(db.Model):
    """Employee file for HR portal (ملف الموظف).

    This model is intentionally wide (single row per user) to support the admin UX:
    save-progress and continue later.
    """
    __tablename__ = "employee_file"

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)

    # Header
    employee_no = db.Column(db.String(50), nullable=True, index=True)
    full_name_quad = db.Column(db.String(255), nullable=True, index=True)

    # Attendance mapping
    timeclock_code = db.Column(db.String(20), nullable=True, index=True)  # 9 digits

    # -----------------------------
    # (1) Basic data - Section 1
    # -----------------------------
    identity_type_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True)
    national_id = db.Column(db.String(50), nullable=True, index=True)
    gender_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True)
    marital_status_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True)
    birth_date = db.Column(db.String(10), nullable=True)  # YYYY-MM-DD
    religion_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True)
    disability_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True)
    home_governorate_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True)
    locality_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True)
    address = db.Column(db.String(255), nullable=True)
    phone = db.Column(db.String(50), nullable=True)
    mobile = db.Column(db.String(50), nullable=True)
    email = db.Column(db.String(255), nullable=True)

    # -----------------------------
    # (1) Basic data - Section 2 (Work Data)
    # -----------------------------
    work_governorate_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True)
    work_location_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True)
    employee_status_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True)
    status_date = db.Column(db.String(10), nullable=True)
    status_note = db.Column(db.String(255), nullable=True)
    shift_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True)
    hourly_number = db.Column(db.Float, nullable=True)  # الرقم في الساعة

    # -----------------------------
    # (1) Basic data - Section 3 (Placement)
    # -----------------------------
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=True, index=True)
    directorate_id = db.Column(db.Integer, db.ForeignKey('directorates.id'), nullable=True, index=True)
    department_id = db.Column(db.Integer, db.ForeignKey('departments.id'), nullable=True, index=True)
    division_id = db.Column(db.Integer, db.ForeignKey('divisions.id'), nullable=True, index=True)

    direct_manager_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)

    project_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True)
    appointment_type_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True)
    hire_date = db.Column(db.String(10), nullable=True)
    last_promotion_date = db.Column(db.String(10), nullable=True)

    job_category_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True)
    job_grade_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True)
    job_title_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True)
    admin_title_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True)

    # -----------------------------
    # (1) Basic data - Section 4 (Bank)
    # -----------------------------
    bank_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True)
    bank_account = db.Column(db.String(100), nullable=True)

    notes = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    user = db.relationship("User", foreign_keys=[user_id], back_populates="employee_file")
    updated_by = db.relationship("User", foreign_keys=[updated_by_id], lazy="joined")

    # Convenience relationships
    organization = db.relationship('Organization', foreign_keys=[organization_id], lazy='joined')
    directorate = db.relationship('Directorate', foreign_keys=[directorate_id], lazy='joined')
    department = db.relationship('Department', foreign_keys=[department_id], lazy='joined')
    division = db.relationship('Division', foreign_keys=[division_id], lazy='joined')

    direct_manager = db.relationship('User', foreign_keys=[direct_manager_user_id], lazy='joined')



class EmployeeAttachment(db.Model):
    __tablename__ = "employee_attachment"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    # Main type buckets (kept for compatibility + special PAYSLIP handling)
    attachment_type = db.Column(db.String(50), nullable=False, default="OTHER")  # PAYSLIP/OTHER

    # Detailed type via lookup (category=ATTACH_TYPE). Optional.
    attachment_type_lookup_id = db.Column(db.Integer, db.ForeignKey("hr_lookup_item.id"), nullable=True, index=True)

    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False)
    note = db.Column(db.String(255), nullable=True)

    # Payslip period (month/year) - optional for backwards compatibility.
    # Used when attachment_type == 'PAYSLIP'.
    payslip_year = db.Column(db.Integer, nullable=True, index=True)
    payslip_month = db.Column(db.Integer, nullable=True, index=True)

    # Publishing control for payslips (draft vs sent/published)
    # Default is False to avoid accidental publishing.
    is_published = db.Column(db.Boolean, default=False, nullable=False, index=True)
    # If True, published will be shown/announced only to employees who match the program conditions.
    published_at = db.Column(db.DateTime, nullable=True)
    published_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    user = db.relationship("User", foreign_keys=[user_id], lazy="joined")
    uploaded_by = db.relationship("User", foreign_keys=[uploaded_by_id], lazy="joined")
    published_by = db.relationship("User", foreign_keys=[published_by_id], lazy="joined")

    attachment_type_lookup = db.relationship("HRLookupItem", foreign_keys=[attachment_type_lookup_id], lazy="joined")

    @property
    def payslip_period_label(self) -> str:
        """Human-friendly month/year label for payslips."""
        try:
            if self.payslip_year and self.payslip_month:
                return f"{int(self.payslip_year):04d}-{int(self.payslip_month):02d}"
        except Exception:
            pass
        return "-"


class EmployeeDependent(db.Model):
    __tablename__ = 'employee_dependent'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)

    full_name = db.Column(db.String(255), nullable=False, default='')
    relation_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True)

    national_id = db.Column(db.String(50), nullable=True)
    gender_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True)
    birth_date = db.Column(db.String(10), nullable=True)  # YYYY-MM-DD
    allowance = db.Column(db.Float, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    user = db.relationship('User', foreign_keys=[user_id], lazy='joined')
    relation_lookup = db.relationship('HRLookupItem', foreign_keys=[relation_lookup_id], lazy='joined')
    gender_lookup = db.relationship('HRLookupItem', foreign_keys=[gender_lookup_id], lazy='joined')
    updated_by = db.relationship('User', foreign_keys=[updated_by_id], lazy='joined')


class EmployeeQualification(db.Model):
    __tablename__ = 'employee_qualification'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)

    degree_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True)
    specialization_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True)
    grade_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True)  # التقدير
    qualification_date = db.Column(db.String(10), nullable=True)  # YYYY-MM-DD

    university_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True)
    country_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True)

    notes = db.Column(db.String(255), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    user = db.relationship('User', foreign_keys=[user_id], lazy='joined')
    degree_lookup = db.relationship('HRLookupItem', foreign_keys=[degree_lookup_id], lazy='joined')
    specialization_lookup = db.relationship('HRLookupItem', foreign_keys=[specialization_lookup_id], lazy='joined')
    grade_lookup = db.relationship('HRLookupItem', foreign_keys=[grade_lookup_id], lazy='joined')
    university_lookup = db.relationship('HRLookupItem', foreign_keys=[university_lookup_id], lazy='joined')
    country_lookup = db.relationship('HRLookupItem', foreign_keys=[country_lookup_id], lazy='joined')
    updated_by = db.relationship('User', foreign_keys=[updated_by_id], lazy='joined')


class EmployeeSecondment(db.Model):
    __tablename__ = 'employee_secondment'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)

    date_from = db.Column(db.String(10), nullable=True)
    date_to = db.Column(db.String(10), nullable=True)

    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=True, index=True)
    directorate_id = db.Column(db.Integer, db.ForeignKey('directorates.id'), nullable=True, index=True)
    department_id = db.Column(db.Integer, db.ForeignKey('departments.id'), nullable=True, index=True)
    division_id = db.Column(db.Integer, db.ForeignKey('divisions.id'), nullable=True, index=True)

    direct_manager_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)

    work_governorate_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True)
    work_location_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True)
    admin_title_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True)

    details = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    user = db.relationship('User', foreign_keys=[user_id], lazy='joined')
    organization = db.relationship('Organization', foreign_keys=[organization_id], lazy='joined')
    directorate = db.relationship('Directorate', foreign_keys=[directorate_id], lazy='joined')
    department = db.relationship('Department', foreign_keys=[department_id], lazy='joined')
    division = db.relationship('Division', foreign_keys=[division_id], lazy='joined')
    direct_manager = db.relationship('User', foreign_keys=[direct_manager_user_id], lazy='joined')

    work_governorate_lookup = db.relationship('HRLookupItem', foreign_keys=[work_governorate_lookup_id], lazy='joined')
    work_location_lookup = db.relationship('HRLookupItem', foreign_keys=[work_location_lookup_id], lazy='joined')
    admin_title_lookup = db.relationship('HRLookupItem', foreign_keys=[admin_title_lookup_id], lazy='joined')

    updated_by = db.relationship('User', foreign_keys=[updated_by_id], lazy='joined')


# =========================================================

# Portal HR: Self-Service Requests (Light Workflow - داخل HR فقط)
# =========================================================

class HRSSWorkflowDefinition(db.Model):
    """Definition for HR self-service request workflows."""
    __tablename__ = "hr_ss_workflow_definition"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, index=True, nullable=False)  # CERTIFICATE / UPDATE_PROFILE / UPLOAD_DOCUMENTS
    name_ar = db.Column(db.String(200), nullable=False, default="")
    name_en = db.Column(db.String(200), nullable=False, default="")
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
class HRSSWorkflowStepDefinition(db.Model):
    """A step inside a definition. Approver can be a role or a specific user."""
    __tablename__ = "hr_ss_workflow_step_definition"

    id = db.Column(db.Integer, primary_key=True)
    definition_id = db.Column(db.Integer, db.ForeignKey("hr_ss_workflow_definition.id"), nullable=False, index=True)
    step_no = db.Column(db.Integer, nullable=False)  # 1..N

    approver_role = db.Column(db.String(50), nullable=True)  # e.g. HR / HR_MANAGER / GENERAL_MANAGER
    approver_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    sla_hours = db.Column(db.Integer, nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    definition = db.relationship("HRSSWorkflowDefinition", backref=db.backref("steps", lazy="selectin"))
    approver_user = db.relationship("User", foreign_keys=[approver_user_id], lazy="joined")

    __table_args__ = (
        db.UniqueConstraint("definition_id", "step_no", name="uq_hr_ss_step_no"),
    )

    # ---- Compatibility aliases (helps older code / quick refactors) ----
    # If any older code uses workflow_id/workflow, keep it working.
    @property
    def workflow_id(self):
        return self.definition_id

    @property
    def workflow(self):
        return self.definition


class HRSSRequest(db.Model):
    """An HR self-service request instance."""
    __tablename__ = "hr_ss_request"

    id = db.Column(db.Integer, primary_key=True)

    requester_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    type_code = db.Column(db.String(50), nullable=False, index=True)  # matches HRSSWorkflowDefinition.code

    status = db.Column(db.String(30), nullable=False, default="DRAFT")  # DRAFT/SUBMITTED/IN_REVIEW/APPROVED/REJECTED/RETURNED/CANCELLED
    current_step_no = db.Column(db.Integer, nullable=True)

    payload_json = db.Column(db.Text, nullable=True)  # JSON string (keep SQLite friendly)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    submitted_at = db.Column(db.DateTime, nullable=True)
    closed_at = db.Column(db.DateTime, nullable=True)

    requester = db.relationship("User", foreign_keys=[requester_id], lazy="joined")

    def payload(self) -> dict:
        try:
            return json.loads(self.payload_json or "{}") or {}
        except Exception:
            return {}


class HRSSRequestApproval(db.Model):
    __tablename__ = "hr_ss_request_approval"

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("hr_ss_request.id"), nullable=False, index=True)
    step_no = db.Column(db.Integer, nullable=False, index=True)

    approver_role = db.Column(db.String(50), nullable=True)
    approver_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    status = db.Column(db.String(20), nullable=False, default="PENDING")  # PENDING/APPROVED/REJECTED/RETURNED/SKIPPED
    note = db.Column(db.Text, nullable=True)

    acted_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    acted_at = db.Column(db.DateTime, nullable=True)

    request = db.relationship("HRSSRequest", backref=db.backref("approvals", lazy="selectin"))
    approver_user = db.relationship("User", foreign_keys=[approver_user_id], lazy="joined")
    acted_by = db.relationship("User", foreign_keys=[acted_by_id], lazy="joined")

    __table_args__ = (
        db.UniqueConstraint("request_id", "step_no", name="uq_hr_ss_req_step"),
    )


class HRSSRequestAttachment(db.Model):
    __tablename__ = "hr_ss_request_attachment"

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("hr_ss_request.id"), nullable=False, index=True)

    doc_type = db.Column(db.String(50), nullable=False, default="OTHER")  # ID_CARD/BANK/CERTIFICATE/OTHER
    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False)
    note = db.Column(db.String(255), nullable=True)


    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    # Optional: who "published"/approved the attachment for official use
    published_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)


    request = db.relationship("HRSSRequest", backref=db.backref("attachments", lazy="selectin"))
    uploaded_by = db.relationship("User", foreign_keys=[uploaded_by_id], lazy="joined")
    published_by = db.relationship("User", foreign_keys=[published_by_id], lazy="joined")


# =========================================================
# Portal HR: Discipline & Legal (خفيف)
# =========================================================

class HRDisciplinaryCase(db.Model):
    __tablename__ = "hr_disciplinary_case"

    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    category = db.Column(db.String(30), nullable=False, default="VIOLATION")  # VIOLATION/WARNING/INVESTIGATION
    severity = db.Column(db.String(10), nullable=False, default="LOW")  # LOW/MED/HIGH
    status = db.Column(db.String(20), nullable=False, default="OPEN")  # OPEN/UNDER_REVIEW/CLOSED

    title = db.Column(db.String(200), nullable=False, default="")
    description = db.Column(db.Text, nullable=True)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    assigned_to_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    # Work location (optional)
    work_governorate_lookup_id = db.Column(db.Integer, db.ForeignKey("hr_lookup_item.id"), nullable=True)
    work_location_lookup_id = db.Column(db.Integer, db.ForeignKey("hr_lookup_item.id"), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    closed_at = db.Column(db.DateTime, nullable=True)

    employee = db.relationship("User", foreign_keys=[employee_id], lazy="joined")
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
    work_governorate = db.relationship("HRLookupItem", foreign_keys=[work_governorate_lookup_id], lazy="joined")
    work_location = db.relationship("HRLookupItem", foreign_keys=[work_location_lookup_id], lazy="joined")
    assigned_to = db.relationship("User", foreign_keys=[assigned_to_id], lazy="joined")


class HRDisciplinaryAction(db.Model):
    __tablename__ = "hr_disciplinary_action"

    id = db.Column(db.Integer, primary_key=True)
    case_id = db.Column(db.Integer, db.ForeignKey("hr_disciplinary_case.id"), nullable=False, index=True)
    action_type = db.Column(db.String(30), nullable=False, default="NOTE")  # WARNING/HEARING/DECISION/NOTE
    note = db.Column(db.Text, nullable=True)
    action_date = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    case = db.relationship("HRDisciplinaryCase", backref=db.backref("actions", lazy="selectin"))
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
class HRDisciplinaryAttachment(db.Model):
    __tablename__ = "hr_disciplinary_attachment"

    id = db.Column(db.Integer, primary_key=True)
    case_id = db.Column(db.Integer, db.ForeignKey("hr_disciplinary_case.id"), nullable=False, index=True)

    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False)
    note = db.Column(db.String(255), nullable=True)

    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    published_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    case = db.relationship("HRDisciplinaryCase", backref=db.backref("attachments", lazy="selectin"))
    uploaded_by = db.relationship("User", foreign_keys=[uploaded_by_id], lazy="joined")
    published_by = db.relationship("User", foreign_keys=[published_by_id], lazy="joined")


# =========================================================
# Portal HR: Documents Portal (سياسات/نماذج)
# =========================================================

class HRDoc(db.Model):
    __tablename__ = "hr_doc"

    id = db.Column(db.Integer, primary_key=True)
    title_ar = db.Column(db.String(200), nullable=False, default="")
    title_en = db.Column(db.String(200), nullable=False, default="")
    category = db.Column(db.String(30), nullable=False, default="POLICY")  # POLICY/FORM/PROCEDURE
    is_published = db.Column(db.Boolean, default=True, nullable=False)

    current_version_id = db.Column(db.Integer, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
class HRDocVersion(db.Model):
    __tablename__ = "hr_doc_version"

    id = db.Column(db.Integer, primary_key=True)
    doc_id = db.Column(db.Integer, db.ForeignKey("hr_doc.id"), nullable=False, index=True)

    version_no = db.Column(db.Integer, nullable=False, default=1)
    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False)

    change_log = db.Column(db.String(255), nullable=True)
    approved_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    approved_at = db.Column(db.DateTime, nullable=True)
    effective_date = db.Column(db.String(10), nullable=True)  # YYYY-MM-DD

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    doc = db.relationship("HRDoc", backref=db.backref("versions", lazy="selectin"))
    approved_by = db.relationship("User", foreign_keys=[approved_by_id], lazy="joined")
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
    __table_args__ = (
        db.UniqueConstraint("doc_id", "version_no", name="uq_hr_doc_version"),
    )



# =========================================================
# Portal HR: Attendance Masterdata (Schedules + Permissions + Leaves)
# =========================================================


class WorkSchedule(db.Model):
    """Work schedule definition.

    Times are stored as 'HH:MM' strings to keep migrations simple.
    For SHIFT schedules, define days in WorkScheduleDay rows.
    """
    __tablename__ = "work_schedule"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    kind = db.Column(db.String(20), nullable=False, default="FIXED")  # FIXED/FLEX/SHIFT/RAMADAN/REMOTE

    # For FIXED/FLEX/RAMADAN/REMOTE
    start_time = db.Column(db.String(5), nullable=True)  # HH:MM
    end_time = db.Column(db.String(5), nullable=True)  # HH:MM

    required_minutes = db.Column(db.Integer, nullable=True)  # for FLEX/REMOTE
    break_minutes = db.Column(db.Integer, default=0, nullable=False)
    grace_minutes = db.Column(db.Integer, default=0, nullable=False)
    overtime_threshold_minutes = db.Column(db.Integer, nullable=True)  # minutes after end_time to start overtime

    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
class WorkScheduleDay(db.Model):
    """Per-day configuration for SHIFT schedules."""
    __tablename__ = "work_schedule_day"

    id = db.Column(db.Integer, primary_key=True)
    schedule_id = db.Column(db.Integer, db.ForeignKey("work_schedule.id"), nullable=False, index=True)
    weekday = db.Column(db.Integer, nullable=False, index=True)  # 0=Mon ... 6=Sun

    start_time = db.Column(db.String(5), nullable=True)
    end_time = db.Column(db.String(5), nullable=True)
    break_minutes = db.Column(db.Integer, default=0, nullable=False)
    grace_minutes = db.Column(db.Integer, default=0, nullable=False)

    schedule = db.relationship("WorkSchedule", backref=db.backref("days", lazy="selectin"))

    __table_args__ = (
        db.UniqueConstraint("schedule_id", "weekday", name="uq_schedule_day"),
    )


class EmployeeScheduleAssignment(db.Model):
    __tablename__ = "employee_schedule_assignment"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    schedule_id = db.Column(db.Integer, db.ForeignKey("work_schedule.id"), nullable=False, index=True)
    start_date = db.Column(db.String(10), nullable=True, index=True)  # YYYY-MM-DD
    end_date = db.Column(db.String(10), nullable=True, index=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    user = db.relationship("User", foreign_keys=[user_id], lazy="joined")
    schedule = db.relationship("WorkSchedule", foreign_keys=[schedule_id], lazy="joined")
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
    __table_args__ = (
        db.Index("ix_emp_schedule_user_active", "user_id", "is_active"),
    )


class WorkPolicy(db.Model):
    """Work policies control *days* and *place* rules.

    This is intentionally lightweight (no heavy rule engine):
      - days_policy: FIXED or HYBRID_WEEKLY_QUOTA
      - fixed_days_mask: bitmask for weekdays (0=Mon ... 6=Sun)
      - hybrid quotas: how many office/remote days per week
      - location_policy: ONSITE / REMOTE / HYBRID
    """
    __tablename__ = "work_policy"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)

    days_policy = db.Column(db.String(40), nullable=False, default="FIXED")
    fixed_days_mask = db.Column(db.Integer, nullable=True)  # bitmask (Mon..Sun)

    hybrid_office_days = db.Column(db.Integer, nullable=True)
    hybrid_remote_days = db.Column(db.Integer, nullable=True)

    location_policy = db.Column(db.String(20), nullable=False, default="ONSITE")

    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
class WorkAssignment(db.Model):
    """Assign a schedule template + policy to a target (user/role/department) within a date range."""
    __tablename__ = "work_assignment"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=True)  # e.g. "اعتيادي" / "عن بعد"

    schedule_id = db.Column(db.Integer, db.ForeignKey("work_schedule.id"), nullable=False, index=True)
    policy_id = db.Column(db.Integer, db.ForeignKey("work_policy.id"), nullable=True, index=True)

    target_type = db.Column(db.String(20), nullable=False)  # USER / ROLE / DEPARTMENT
    target_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    target_role = db.Column(db.String(80), nullable=True, index=True)
    target_department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=True, index=True)

    start_date = db.Column(db.String(10), nullable=True, index=True)  # YYYY-MM-DD
    end_date = db.Column(db.String(10), nullable=True, index=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    schedule = db.relationship("WorkSchedule", foreign_keys=[schedule_id], lazy="joined")
    policy = db.relationship("WorkPolicy", foreign_keys=[policy_id], lazy="joined")
    user = db.relationship("User", foreign_keys=[target_user_id], lazy="joined")
    department = db.relationship("Department", foreign_keys=[target_department_id], lazy="joined")
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
    __table_args__ = (
        db.Index("ix_work_assignment_active", "is_active", "target_type"),
    )


class HRPermissionType(db.Model):
    """Types of permissions/moghaderat (مغادرة/إذن)."""
    __tablename__ = "hr_permission_type"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), nullable=False, unique=True, index=True)
    name_ar = db.Column(db.String(200), nullable=False)
    name_en = db.Column(db.String(200), nullable=True)
    requires_approval = db.Column(db.Boolean, default=True, nullable=False)
    max_hours = db.Column(db.Integer, nullable=True)
    counts_as_work = db.Column(db.Boolean, default=False, nullable=False)  # if true, doesn't reduce work minutes
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
class HRLeaveType(db.Model):
    """Leave types (إجازات)."""
    __tablename__ = "hr_leave_type"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), nullable=False, unique=True, index=True)
    name_ar = db.Column(db.String(200), nullable=False)
    name_en = db.Column(db.String(200), nullable=True)
    requires_approval = db.Column(db.Boolean, default=True, nullable=False)
    max_days = db.Column(db.Integer, nullable=True)
    # Default annual balance (days). Optional; used for leave balance reports/alerts.
    default_balance_days = db.Column(db.Integer, nullable=True)
    # Optional: exceptional maximum duration (e.g., chronic sick leave extension).
    # If set, a request may exceed max_days up to this limit, but typically requires HR approval.
    exception_max_days = db.Column(db.Integer, nullable=True)
    exception_requires_hr = db.Column(db.Boolean, default=True, nullable=False)
    # When approving an exceptional duration, require a decision note (useful for medical cases).
    exception_requires_note = db.Column(db.Boolean, default=False, nullable=False)
    # Supporting documents (e.g., medical report).
    requires_documents = db.Column(db.Boolean, default=False, nullable=False)
    documents_hint = db.Column(db.String(255), nullable=True)
    is_external = db.Column(db.Boolean, default=False, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
class HRLeaveGradeEntitlement(db.Model):
    """Per-grade annual entitlement (allowed days) per leave type.

    Example: PERSONAL leave = 30/31/32 depending on administrative grade.
    """
    __tablename__ = "hr_leave_grade_entitlement"

    id = db.Column(db.Integer, primary_key=True)
    leave_type_id = db.Column(db.Integer, db.ForeignKey("hr_leave_type.id"), nullable=False, index=True)
    grade = db.Column(db.String(50), nullable=False, index=True)
    allowed_days = db.Column(db.Integer, nullable=False, default=0)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    __table_args__ = (
        db.UniqueConstraint("leave_type_id", "grade", name="uq_leave_grade_entitlement"),
    )


# =========================================================
# Portal HR: Employee Requests (Leaves / Permissions)
# =========================================================


class HRLeaveRequest(db.Model):
    """Employee leave request (طلب إجازة).

    Stored as simple strings (YYYY-MM-DD) to keep migrations easy.
    """

    __tablename__ = "hr_leave_request"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    leave_type_id = db.Column(db.Integer, db.ForeignKey("hr_leave_type.id"), nullable=False, index=True)

    start_date = db.Column(db.String(10), nullable=False, index=True)  # YYYY-MM-DD
    end_date = db.Column(db.String(10), nullable=False, index=True)  # YYYY-MM-DD
    days = db.Column(db.Integer, nullable=True)

    # --- Admin portal fields ---
    leave_place = db.Column(db.String(20), nullable=True, index=True)  # INTERNAL/EXTERNAL
    entered_by = db.Column(db.String(20), nullable=False, default='UNSPEC', index=True)  # SELF/ADMIN/UNSPEC
    created_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    admin_status_id = db.Column(db.Integer, db.ForeignKey('hr_status_def.id'), nullable=True, index=True)

    # External leave fields (optional)
    travel_country = db.Column(db.String(120), nullable=True)
    travel_city = db.Column(db.String(120), nullable=True)
    travel_address = db.Column(db.String(255), nullable=True)
    travel_contact_phone = db.Column(db.String(50), nullable=True)
    travel_purpose = db.Column(db.Text, nullable=True)
    border_crossing = db.Column(db.String(120), nullable=True)

    note = db.Column(db.Text, nullable=True)

    # DRAFT/SUBMITTED/APPROVED/REJECTED/CANCELLED
    status = db.Column(db.String(20), default="SUBMITTED", nullable=False, index=True)

    submitted_at = db.Column(db.DateTime, nullable=True)
    decided_at = db.Column(db.DateTime, nullable=True)

    approver_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    decided_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    decision_note = db.Column(db.Text, nullable=True)
    # Reminders for pending approvals
    reminder_sent_at = db.Column(db.DateTime, nullable=True)
    reminder_count = db.Column(db.Integer, nullable=False, default=0)

    # Cancellation / stop future deduction
    cancelled_at = db.Column(db.DateTime, nullable=True)
    cancelled_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    cancelled_from_status = db.Column(db.String(20), nullable=True)
    cancel_note = db.Column(db.Text, nullable=True)
    # Inclusive last day to count as used when cancelled (YYYY-MM-DD).
    cancel_effective_date = db.Column(db.String(10), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User", foreign_keys=[user_id], lazy="joined")
    leave_type = db.relationship("HRLeaveType", foreign_keys=[leave_type_id], lazy="joined")
    approver_user = db.relationship("User", foreign_keys=[approver_user_id], lazy="joined")
    decided_by = db.relationship("User", foreign_keys=[decided_by_id], lazy="joined")
    cancelled_by_user = db.relationship("User", foreign_keys=[cancelled_by_id], lazy="joined")
    created_by = db.relationship('User', foreign_keys=[created_by_id], lazy='joined')
    admin_status = db.relationship('HRStatusDef', foreign_keys=[admin_status_id], lazy='joined')

    attachments = db.relationship("HRLeaveAttachment", back_populates="request", cascade="all, delete-orphan", lazy="selectin")

    __table_args__ = (
        db.Index("ix_hr_leave_req_user_status", "user_id", "status"),
        db.Index("ix_hr_leave_req_approver_status", "approver_user_id", "status"),
    )




class HRLeaveAttachment(db.Model):
    """Attachments for leave requests (e.g., medical reports).

    Stored under instance/uploads/leaves/<request_id>/
    """

    __tablename__ = 'hr_leave_attachment'

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey('hr_leave_request.id'), nullable=False, index=True)

    doc_type = db.Column(db.String(50), nullable=True)  # e.g., REPORT
    original_name = db.Column(db.String(255), nullable=True)
    stored_name = db.Column(db.String(255), nullable=True)

    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)

    request = db.relationship('HRLeaveRequest', back_populates='attachments', lazy='joined')
    uploaded_by = db.relationship('User', foreign_keys=[uploaded_by_id], lazy='joined')

    __table_args__ = (
        db.Index('ix_hr_leave_att_req', 'request_id'),
    )



class HRLeaveBalance(db.Model):
    __tablename__ = 'hr_leave_balance'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    leave_type_id = db.Column(db.Integer, db.ForeignKey('hr_leave_type.id'), nullable=False, index=True)
    year = db.Column(db.Integer, nullable=False, index=True)

    # Entitlement days for the year
    total_days = db.Column(db.Integer, nullable=False, default=0)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = db.relationship('User', lazy='selectin')
    leave_type = db.relationship('HRLeaveType', lazy='selectin')

    __table_args__ = (
        db.UniqueConstraint('user_id', 'leave_type_id', 'year', name='uq_hr_leave_balance_user_type_year'),
    )

class HRPermissionRequest(db.Model):
    """Employee permission/moghadera request (طلب مغادرة/إذن)."""

    __tablename__ = "hr_permission_request"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    permission_type_id = db.Column(db.Integer, db.ForeignKey("hr_permission_type.id"), nullable=False, index=True)

    day = db.Column(db.String(10), nullable=False, index=True)  # YYYY-MM-DD
    from_time = db.Column(db.String(5), nullable=True)  # HH:MM
    to_time = db.Column(db.String(5), nullable=True)  # HH:MM
    hours = db.Column(db.Integer, nullable=True)

    note = db.Column(db.Text, nullable=True)

    # DRAFT/SUBMITTED/APPROVED/REJECTED/CANCELLED
    status = db.Column(db.String(20), default="SUBMITTED", nullable=False, index=True)

    submitted_at = db.Column(db.DateTime, nullable=True)
    decided_at = db.Column(db.DateTime, nullable=True)

    approver_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    decided_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    decision_note = db.Column(db.Text, nullable=True)

    # Cancellation (optional)
    cancelled_at = db.Column(db.DateTime, nullable=True)
    cancelled_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    cancelled_from_status = db.Column(db.String(20), nullable=True)

    # Created by (admin who entered the permission on behalf of an employee, or the employee themselves)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    # Optional attachment
    attachment_name = db.Column(db.String(255), nullable=True)
    attachment_path = db.Column(db.String(400), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User", foreign_keys=[user_id], lazy="joined")
    permission_type = db.relationship("HRPermissionType", foreign_keys=[permission_type_id], lazy="joined")
    approver_user = db.relationship("User", foreign_keys=[approver_user_id], lazy="joined")
    decided_by = db.relationship("User", foreign_keys=[decided_by_id], lazy="joined")
    cancelled_by_user = db.relationship("User", foreign_keys=[cancelled_by_id], lazy="joined")
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
    __table_args__ = (
        db.Index("ix_hr_perm_req_user_status", "user_id", "status"),
        db.Index("ix_hr_perm_req_approver_status", "approver_user_id", "status"),
    )


class HRMonthlyPermissionAllowance(db.Model):
    """Per-employee allowed permission hours per month.

    Used by the monthly leave report to exempt a number of hours from deduction.
    """

    __tablename__ = "hr_monthly_permission_allowance"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    year = db.Column(db.Integer, nullable=False, index=True)
    month = db.Column(db.Integer, nullable=False, index=True)

    allowed_hours = db.Column(db.Integer, nullable=False, default=0)

    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    user = db.relationship("User", foreign_keys=[user_id], lazy="joined")
    updated_by = db.relationship("User", foreign_keys=[updated_by_id], lazy="joined")

    __table_args__ = (
        db.UniqueConstraint("user_id", "year", "month", name="uq_hr_perm_allow_user_year_month"),
        db.CheckConstraint("month >= 1 AND month <= 12", name="ck_hr_perm_allow_month"),
    )


class AttendanceDailySummary(db.Model):
    """Computed daily attendance KPIs per employee."""
    __tablename__ = "attendance_daily_summary"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    # Date range (from/to). If day_to is NULL, it is a single-day record.
    day = db.Column(db.String(10), nullable=False, index=True)  # YYYY-MM-DD (from)
    day_to = db.Column(db.String(10), nullable=True, index=True)  # YYYY-MM-DD (to)

    # Target kind (future-proof). Currently we support USER only.
    target_kind = db.Column(db.String(20), nullable=True, default="USER")

    schedule_id = db.Column(db.Integer, db.ForeignKey("work_schedule.id"), nullable=True)

    first_in = db.Column(db.DateTime, nullable=True)
    last_out = db.Column(db.DateTime, nullable=True)

    work_minutes = db.Column(db.Integer, default=0, nullable=False)
    break_minutes = db.Column(db.Integer, default=0, nullable=False)
    late_minutes = db.Column(db.Integer, default=0, nullable=False)
    early_leave_minutes = db.Column(db.Integer, default=0, nullable=False)
    overtime_minutes = db.Column(db.Integer, default=0, nullable=False)

    status = db.Column(db.String(20), default="OK", nullable=False)  # OK/INCOMPLETE/ABSENT
    computed_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User", foreign_keys=[user_id], lazy="joined")
    schedule = db.relationship("WorkSchedule", foreign_keys=[schedule_id], lazy="joined")

    __table_args__ = (
        db.UniqueConstraint("user_id", "day", name="uq_att_daily_user_day"),
        db.Index("ix_att_daily_day", "day"),
    )


class AttendanceImportBatch(db.Model):
    """Raw timeclock import batch (manual upload or auto sync)."""

    __tablename__ = "attendance_import_batch"

    id = db.Column(db.Integer, primary_key=True)

    filename = db.Column(db.String(255), nullable=False)

    imported_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    imported_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    total_lines = db.Column(db.Integer, default=0, nullable=False)
    inserted = db.Column(db.Integer, default=0, nullable=False)
    skipped = db.Column(db.Integer, default=0, nullable=False)

    # Store a short error summary (first N lines) to keep the batch page useful.
    errors = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    imported_by = db.relationship("User", foreign_keys=[imported_by_id], lazy="joined")

    events = db.relationship(
        "AttendanceEvent",
        back_populates="batch",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<AttendanceImportBatch id={self.id} file={self.filename!r} at={self.imported_at}>"


class AttendanceEvent(db.Model):
    """Single raw timeclock event (IN/OUT) mapped to a user."""

    __tablename__ = "attendance_event"

    id = db.Column(db.Integer, primary_key=True)

    batch_id = db.Column(db.Integer, db.ForeignKey("attendance_import_batch.id"), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    event_dt = db.Column(db.DateTime, nullable=False, index=True)
    # Common values: I / O (or IN / OUT). Keep it flexible.
    event_type = db.Column(db.String(10), nullable=False, index=True)
    device_id = db.Column(db.String(20), nullable=True, index=True)

    raw_line = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User", foreign_keys=[user_id], lazy="joined")
    batch = db.relationship("AttendanceImportBatch", foreign_keys=[batch_id], back_populates="events", lazy="joined")

    __table_args__ = (
        # Prevent duplicates across sync/import
        db.UniqueConstraint("user_id", "event_dt", "event_type", "device_id", name="uq_att_event_key"),
        db.Index("ix_att_event_user_dt", "user_id", "event_dt"),
        db.Index("ix_att_event_batch_dt", "batch_id", "event_dt"),
    )

    def __repr__(self) -> str:
        return f"<AttendanceEvent id={self.id} user={self.user_id} dt={self.event_dt} type={self.event_type}>"




# =========================================================
# Portal HR: Attendance Special Cases / Deductions / Holidays / Rooms
# =========================================================

class HRAttendanceSpecialCase(db.Model):
    """Manual overrides / special cases for attendance.

    Used by HR pages:
      - إدخال حالة (STATUS): overrides AttendanceDailySummary.status
      - إدخال استثناء (EXCEPTION): overrides a numeric field (late/early/work/overtime)

    Note: We keep this table flexible (no hard constraints) to avoid migration friction.
    """

    __tablename__ = "hr_att_special_case"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    # Date range (from/to). If day_to is NULL, it is a single-day record.
    day = db.Column(db.String(10), nullable=False, index=True)  # YYYY-MM-DD (from)
    day_to = db.Column(db.String(10), nullable=True, index=True)  # YYYY-MM-DD (to)

    # Target kind (future-proof). Currently we support USER only.
    target_kind = db.Column(db.String(20), nullable=True, default="USER")

    kind = db.Column(db.String(20), nullable=False, default="STATUS")  # STATUS/EXCEPTION

    # STATUS override
    status = db.Column(db.String(30), nullable=True)

    # Optional time window + allowances (as shown in the reference screens)
    start_time = db.Column(db.String(5), nullable=True)  # HH:MM
    end_time = db.Column(db.String(5), nullable=True)    # HH:MM
    allow_morning_minutes = db.Column(db.Integer, nullable=True)
    allow_evening_minutes = db.Column(db.Integer, nullable=True)

    # EXCEPTION override
    field = db.Column(db.String(40), nullable=True)  # LATE_MINUTES/EARLY_LEAVE_MINUTES/WORK_MINUTES/OVERTIME_MINUTES
    value_int = db.Column(db.Integer, nullable=True)

    note = db.Column(db.Text, nullable=True)
    applied = db.Column(db.Boolean, default=True, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    user = db.relationship("User", foreign_keys=[user_id], lazy="joined")
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
    __table_args__ = (
        db.Index("ix_hr_att_special_user_day", "user_id", "day"),
    )


class HRAttendanceClosing(db.Model):
    """Mass closing periods for attendance (الإغلاق الجماعي)."""

    __tablename__ = "hr_att_closing"

    id = db.Column(db.Integer, primary_key=True)

    day_from = db.Column(db.String(10), nullable=False, index=True)  # YYYY-MM-DD
    day_to = db.Column(db.String(10), nullable=False, index=True)

    # Filters/scope (optional): match the reference system screens
    work_governorate_lookup_id = db.Column(db.Integer, db.ForeignKey("hr_lookup_item.id"), nullable=True, index=True)
    work_location_lookup_id = db.Column(db.Integer, db.ForeignKey("hr_lookup_item.id"), nullable=True, index=True)

    # Close which movements? (attendance + permissions)
    close_attendance = db.Column(db.Boolean, default=True, nullable=False)
    close_permissions = db.Column(db.Boolean, default=True, nullable=False)

    note = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
    work_governorate = db.relationship("HRLookupItem", foreign_keys=[work_governorate_lookup_id], lazy="joined")
    work_location = db.relationship("HRLookupItem", foreign_keys=[work_location_lookup_id], lazy="joined")

class HRAttendanceDeductionConfig(db.Model):
    """Settings for attendance-based deductions (إعدادات الخصم)."""

    __tablename__ = "hr_att_deduction_config"

    id = db.Column(db.Integer, primary_key=True)

    # Monetary value per minute (legacy/simple model).
    late_minute_value = db.Column(db.Float, nullable=True)
    early_minute_value = db.Column(db.Float, nullable=True)

    # Optional: flat amount per absent day (legacy).
    absent_day_value = db.Column(db.Float, nullable=True)

    # Reference-system settings (7 hours -> 1 day)
    deduction_style = db.Column(db.String(40), default="AGGREGATE", nullable=False)  # AGGREGATE / PER_CATEGORY
    hours_per_day = db.Column(db.Float, default=7.0, nullable=False)

    late_source = db.Column(db.String(20), default="SALARY", nullable=False)
    early_source = db.Column(db.String(20), default="SALARY", nullable=False)
    special_permission_source = db.Column(db.String(20), default="SALARY", nullable=False)
    unauthorized_permission_source = db.Column(db.String(20), default="SALARY", nullable=False)

    carry_method = db.Column(db.String(40), default="CARRY_TO_NEXT", nullable=False)  # CARRY_TO_NEXT / WITHIN_MONTH

    currency = db.Column(db.String(10), default="ILS", nullable=False)

    note = db.Column(db.Text, nullable=True)

    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    updated_by = db.relationship("User", foreign_keys=[updated_by_id], lazy="joined")


class HRAttendanceDeductionRun(db.Model):
    """A single deduction execution (تنفيذ الخصم) for a month."""

    __tablename__ = "hr_att_deduction_run"

    id = db.Column(db.Integer, primary_key=True)

    year = db.Column(db.Integer, nullable=False, index=True)
    month = db.Column(db.Integer, nullable=False, index=True)

    status = db.Column(db.String(20), default="DRAFT", nullable=False)  # DRAFT/FINAL
    note = db.Column(db.Text, nullable=True)

    totals_json = db.Column(db.Text, nullable=True)  # summary JSON string

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
class HRAttendanceDeductionItem(db.Model):
    """Per-employee deduction totals linked to a run."""

    __tablename__ = "hr_att_deduction_item"

    id = db.Column(db.Integer, primary_key=True)

    run_id = db.Column(db.Integer, db.ForeignKey("hr_att_deduction_run.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    late_minutes = db.Column(db.Integer, default=0, nullable=False)
    early_leave_minutes = db.Column(db.Integer, default=0, nullable=False)
    absent_days = db.Column(db.Integer, default=0, nullable=False)

    amount = db.Column(db.Float, default=0.0, nullable=False)

    note = db.Column(db.Text, nullable=True)

    run = db.relationship("HRAttendanceDeductionRun", backref=db.backref("items", lazy="selectin", cascade="all, delete-orphan"))
    user = db.relationship("User", foreign_keys=[user_id], lazy="joined")

    __table_args__ = (
        db.UniqueConstraint("run_id", "user_id", name="uq_hr_att_deduct_run_user"),
    )


class HROfficialMission(db.Model):
    """Official mission/assignment that may impact attendance and reports."""

    __tablename__ = "hr_official_mission"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    title = db.Column(db.String(200), nullable=False, default="")
    start_day = db.Column(db.String(10), nullable=False, index=True)  # YYYY-MM-DD
    end_day = db.Column(db.String(10), nullable=False, index=True)

    days = db.Column(db.Integer, nullable=True)
    entered_by = db.Column(db.String(20), nullable=False, default='UNSPEC', index=True)  # SELF/ADMIN/UNSPEC
    status_def_id = db.Column(db.Integer, db.ForeignKey('hr_status_def.id'), nullable=True, index=True)

    destination = db.Column(db.String(200), nullable=True)
    note = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    user = db.relationship("User", foreign_keys=[user_id], lazy="joined")
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
    status_def = db.relationship('HRStatusDef', foreign_keys=[status_def_id], lazy='joined')
    attachments = db.relationship('HROfficialMissionAttachment', back_populates='mission', cascade='all, delete-orphan', lazy='selectin')
class HROfficialOccasion(db.Model):
    """Official occasions (holidays/events)."""

    __tablename__ = "hr_official_occasion"

    id = db.Column(db.Integer, primary_key=True)

    title = db.Column(db.String(200), nullable=False, default="")
    day = db.Column(db.String(10), nullable=False, unique=True, index=True)  # YYYY-MM-DD

    is_day_off = db.Column(db.Boolean, default=True, nullable=False)

    note = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
class HRRoom(db.Model):
    __tablename__ = "hr_room"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False, unique=True)
    location = db.Column(db.String(200), nullable=True)
    capacity = db.Column(db.Integer, nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class HRRoomBooking(db.Model):
    __tablename__ = "hr_room_booking"

    id = db.Column(db.Integer, primary_key=True)

    room_id = db.Column(db.Integer, db.ForeignKey("hr_room.id"), nullable=False, index=True)
    booked_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    title = db.Column(db.String(200), nullable=False, default="")
    day = db.Column(db.String(10), nullable=False, index=True)  # YYYY-MM-DD
    start_time = db.Column(db.String(5), nullable=True)  # HH:MM
    end_time = db.Column(db.String(5), nullable=True)

    status = db.Column(db.String(20), default="CONFIRMED", nullable=False)  # CONFIRMED/CANCELLED
    note = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    room = db.relationship("HRRoom", foreign_keys=[room_id], lazy="joined")
    booked_by = db.relationship("User", foreign_keys=[booked_by_id], lazy="joined")

# ======================
# Portal Store (Repository)
# ======================
class StoreCategory(db.Model):
    """Simple classification used by the Portal Store module."""
    __tablename__ = "store_category"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    # Folder hierarchy (optional). Root categories have parent_id = NULL.
    parent_id = db.Column(db.Integer, db.ForeignKey("store_category.id"), nullable=True, index=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    parent = db.relationship("StoreCategory", remote_side=[id], lazy="joined")

    __table_args__ = (
        db.Index("ix_store_category_active", "is_active"),
    )


class StoreFile(db.Model):
    """A lightweight repository file record for the portal."""
    __tablename__ = "store_file"

    id = db.Column(db.Integer, primary_key=True)

    title = db.Column(db.String(255), nullable=True)
    description = db.Column(db.Text, nullable=True)

    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(500), nullable=False)

    mime_type = db.Column(db.String(120), nullable=True)
    file_size = db.Column(db.Integer, nullable=True)

    category_id = db.Column(db.Integer, db.ForeignKey("store_category.id"), nullable=True, index=True)
    uploader_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    is_deleted = db.Column(db.Boolean, default=False, nullable=False)
    deleted_at = db.Column(db.DateTime, nullable=True)
    deleted_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    category = db.relationship("StoreCategory", foreign_keys=[category_id], lazy="joined")
    uploader = db.relationship("User", foreign_keys=[uploader_id], lazy="joined")
    deleted_by_user = db.relationship("User", foreign_keys=[deleted_by], lazy="joined")

    @property
    def display_name(self) -> str:
        return (self.title or self.original_name or "")

    @property
    def file_ext(self) -> str:
        n = self.original_name or self.stored_name or ""
        if "." in n:
            return n.rsplit(".", 1)[1].upper()
        return ""


class StoreFilePermission(db.Model):
    """Share a store file with a user or a role.

    Notes:
    - If user_id is set, it applies to that specific user.
    - If role is set, it applies to users having that role.
    - Either user_id or role must be provided.
    """
    __tablename__ = "store_file_permission"

    id = db.Column(db.Integer, primary_key=True)

    file_id = db.Column(db.Integer, db.ForeignKey("store_file.id"), nullable=False, index=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    role = db.Column(db.String(64), nullable=True, index=True)  # normalized to upper

    can_download = db.Column(db.Boolean, default=True, nullable=False)

    shared_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    shared_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=True)

    file = db.relationship("StoreFile", foreign_keys=[file_id], lazy="joined")
    user = db.relationship("User", foreign_keys=[user_id], lazy="joined")
    shared_by_user = db.relationship("User", foreign_keys=[shared_by], lazy="joined")

    __table_args__ = (
        db.Index("ix_store_file_perm_file_user", "file_id", "user_id"),
        db.Index("ix_store_file_perm_file_role", "file_id", "role"),
    )





# ======================
# Inventory Store (Warehouse Module)
# ======================
class InvWarehouse(db.Model):
    __tablename__ = "inv_warehouse"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, index=True)
    code = db.Column(db.String(50), nullable=True, index=True)
    note = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    @property
    def label(self) -> str:
        return (self.name or self.code or str(self.id)).strip()


class InvItemCategory(db.Model):
    __tablename__ = "inv_item_category"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, index=True, unique=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    @property
    def label(self) -> str:
        return (self.name or str(self.id)).strip()


class InvItem(db.Model):
    __tablename__ = "inv_item"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False, index=True)
    code = db.Column(db.String(80), nullable=True, index=True)
    unit = db.Column(db.String(80), nullable=True)

    category_id = db.Column(db.Integer, db.ForeignKey("inv_item_category.id"), nullable=True, index=True)

    note = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    category = db.relationship("InvItemCategory", foreign_keys=[category_id], lazy="joined")

    @property
    def label(self) -> str:
        base = (self.name or '').strip() or (self.code or '').strip() or str(self.id)
        if self.code and self.code.strip() and self.code.strip() not in base:
            return f"{base} ({self.code.strip()})"
        return base


class InvIssueVoucher(db.Model):
    __tablename__ = "inv_issue_voucher"

    id = db.Column(db.Integer, primary_key=True)

    # ROOM / WAREHOUSE / EMPLOYEE (future)
    issue_kind = db.Column(db.String(20), nullable=False, default="ROOM", index=True)

    voucher_no = db.Column(db.String(80), nullable=False, index=True)
    voucher_date = db.Column(db.String(10), nullable=False, index=True)  # YYYY-MM-DD

    from_warehouse_id = db.Column(db.Integer, db.ForeignKey("inv_warehouse.id"), nullable=False, index=True)
    to_warehouse_id = db.Column(db.Integer, db.ForeignKey("inv_warehouse.id"), nullable=True, index=True)

    to_room_name = db.Column(db.String(200), nullable=True)

    note = db.Column(db.Text, nullable=True)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    from_warehouse = db.relationship("InvWarehouse", foreign_keys=[from_warehouse_id], lazy="joined")
    to_warehouse = db.relationship("InvWarehouse", foreign_keys=[to_warehouse_id], lazy="joined")
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")

    __table_args__ = (
        db.Index("ix_inv_issue_voucher_date_no", "voucher_date", "voucher_no"),
    )


class InvIssueVoucherLine(db.Model):
    __tablename__ = "inv_issue_voucher_line"

    id = db.Column(db.Integer, primary_key=True)
    voucher_id = db.Column(db.Integer, db.ForeignKey("inv_issue_voucher.id"), nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey("inv_item.id"), nullable=False, index=True)

    qty = db.Column(db.Float, nullable=False, default=1.0)
    serial = db.Column(db.String(200), nullable=True)

    warranty_start = db.Column(db.String(10), nullable=True)  # YYYY-MM-DD
    warranty_end = db.Column(db.String(10), nullable=True)

    details = db.Column(db.Text, nullable=True)

    voucher = db.relationship("InvIssueVoucher", foreign_keys=[voucher_id], lazy="joined")
    item = db.relationship("InvItem", foreign_keys=[item_id], lazy="joined")


class InvIssueVoucherAttachment(db.Model):
    __tablename__ = "inv_issue_voucher_attachment"

    id = db.Column(db.Integer, primary_key=True)
    voucher_id = db.Column(db.Integer, db.ForeignKey("inv_issue_voucher.id"), nullable=False, index=True)

    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(500), nullable=False)

    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    voucher = db.relationship("InvIssueVoucher", foreign_keys=[voucher_id], lazy="joined")
    uploaded_by = db.relationship("User", foreign_keys=[uploaded_by_id], lazy="joined")




# ----------------------
# Inventory: Inbound (Input) Vouchers
# ----------------------


class InvInboundVoucher(db.Model):
    __tablename__ = "inv_inbound_voucher"

    id = db.Column(db.Integer, primary_key=True)

    voucher_no = db.Column(db.String(80), nullable=False, index=True)
    voucher_date = db.Column(db.String(10), nullable=False, index=True)  # YYYY-MM-DD

    to_warehouse_id = db.Column(db.Integer, db.ForeignKey("inv_warehouse.id"), nullable=False, index=True)

    note = db.Column(db.Text, nullable=True)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    to_warehouse = db.relationship("InvWarehouse", foreign_keys=[to_warehouse_id], lazy="joined")
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")

    __table_args__ = (
        db.Index("ix_inv_inbound_voucher_date_no", "voucher_date", "voucher_no"),
    )


class InvInboundVoucherLine(db.Model):
    __tablename__ = "inv_inbound_voucher_line"

    id = db.Column(db.Integer, primary_key=True)
    voucher_id = db.Column(db.Integer, db.ForeignKey("inv_inbound_voucher.id"), nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey("inv_item.id"), nullable=False, index=True)

    qty = db.Column(db.Float, nullable=False, default=1.0)
    serial = db.Column(db.String(200), nullable=True)

    warranty_start = db.Column(db.String(10), nullable=True)  # YYYY-MM-DD
    warranty_end = db.Column(db.String(10), nullable=True)

    details = db.Column(db.Text, nullable=True)

    voucher = db.relationship("InvInboundVoucher", foreign_keys=[voucher_id], lazy="joined")
    item = db.relationship("InvItem", foreign_keys=[item_id], lazy="joined")


class InvInboundVoucherAttachment(db.Model):
    __tablename__ = "inv_inbound_voucher_attachment"

    id = db.Column(db.Integer, primary_key=True)
    voucher_id = db.Column(db.Integer, db.ForeignKey("inv_inbound_voucher.id"), nullable=False, index=True)

    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(500), nullable=False)

    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    voucher = db.relationship("InvInboundVoucher", foreign_keys=[voucher_id], lazy="joined")
    uploaded_by = db.relationship("User", foreign_keys=[uploaded_by_id], lazy="joined")


# ----------------------
# Inventory: Scrap (Destruction) Vouchers
# ----------------------


class InvScrapVoucher(db.Model):
    __tablename__ = "inv_scrap_voucher"

    id = db.Column(db.Integer, primary_key=True)

    voucher_no = db.Column(db.String(80), nullable=False, index=True)
    voucher_date = db.Column(db.String(10), nullable=False, index=True)  # YYYY-MM-DD

    from_warehouse_id = db.Column(db.Integer, db.ForeignKey("inv_warehouse.id"), nullable=False, index=True)
    note = db.Column(db.Text, nullable=True)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    from_warehouse = db.relationship("InvWarehouse", foreign_keys=[from_warehouse_id], lazy="joined")
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")

    __table_args__ = (
        db.Index("ix_inv_scrap_voucher_date_no", "voucher_date", "voucher_no"),
    )


class InvScrapVoucherLine(db.Model):
    __tablename__ = "inv_scrap_voucher_line"

    id = db.Column(db.Integer, primary_key=True)
    voucher_id = db.Column(db.Integer, db.ForeignKey("inv_scrap_voucher.id"), nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey("inv_item.id"), nullable=False, index=True)

    qty = db.Column(db.Float, nullable=False, default=1.0)
    serial = db.Column(db.String(200), nullable=True)
    warranty_start = db.Column(db.String(10), nullable=True)  # YYYY-MM-DD
    warranty_end = db.Column(db.String(10), nullable=True)
    details = db.Column(db.Text, nullable=True)

    voucher = db.relationship("InvScrapVoucher", foreign_keys=[voucher_id], lazy="joined")
    item = db.relationship("InvItem", foreign_keys=[item_id], lazy="joined")


class InvScrapVoucherAttachment(db.Model):
    __tablename__ = "inv_scrap_voucher_attachment"

    id = db.Column(db.Integer, primary_key=True)
    voucher_id = db.Column(db.Integer, db.ForeignKey("inv_scrap_voucher.id"), nullable=False, index=True)

    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(500), nullable=False)

    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    voucher = db.relationship("InvScrapVoucher", foreign_keys=[voucher_id], lazy="joined")
    uploaded_by = db.relationship("User", foreign_keys=[uploaded_by_id], lazy="joined")


# ----------------------
# Inventory: Return Vouchers
# ----------------------


class InvReturnVoucher(db.Model):
    __tablename__ = "inv_return_voucher"

    id = db.Column(db.Integer, primary_key=True)

    voucher_no = db.Column(db.String(80), nullable=False, index=True)
    voucher_date = db.Column(db.String(10), nullable=False, index=True)  # YYYY-MM-DD

    to_warehouse_id = db.Column(db.Integer, db.ForeignKey("inv_warehouse.id"), nullable=False, index=True)
    from_room_name = db.Column(db.String(200), nullable=False)

    note = db.Column(db.Text, nullable=True)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    to_warehouse = db.relationship("InvWarehouse", foreign_keys=[to_warehouse_id], lazy="joined")
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")

    __table_args__ = (
        db.Index("ix_inv_return_voucher_date_no", "voucher_date", "voucher_no"),
    )


class InvReturnVoucherLine(db.Model):
    __tablename__ = "inv_return_voucher_line"

    id = db.Column(db.Integer, primary_key=True)
    voucher_id = db.Column(db.Integer, db.ForeignKey("inv_return_voucher.id"), nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey("inv_item.id"), nullable=False, index=True)

    qty = db.Column(db.Float, nullable=False, default=1.0)
    serial = db.Column(db.String(200), nullable=True)
    warranty_start = db.Column(db.String(10), nullable=True)  # YYYY-MM-DD
    warranty_end = db.Column(db.String(10), nullable=True)
    details = db.Column(db.Text, nullable=True)

    voucher = db.relationship("InvReturnVoucher", foreign_keys=[voucher_id], lazy="joined")
    item = db.relationship("InvItem", foreign_keys=[item_id], lazy="joined")


class InvReturnVoucherAttachment(db.Model):
    __tablename__ = "inv_return_voucher_attachment"

    id = db.Column(db.Integer, primary_key=True)
    voucher_id = db.Column(db.Integer, db.ForeignKey("inv_return_voucher.id"), nullable=False, index=True)

    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(500), nullable=False)

    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    voucher = db.relationship("InvReturnVoucher", foreign_keys=[voucher_id], lazy="joined")
    uploaded_by = db.relationship("User", foreign_keys=[uploaded_by_id], lazy="joined")


class InvRequest(db.Model):
    __tablename__ = "inv_request"

    id = db.Column(db.Integer, primary_key=True)

    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=True, index=True)
    work_location_lookup_id = db.Column(db.Integer, db.ForeignKey("hr_lookup_item.id"), nullable=True, index=True)

    entered_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    request_date = db.Column(db.String(10), nullable=False, index=True)  # YYYY-MM-DD

    items_text = db.Column(db.Text, nullable=True)  # "الطلبية تحتوي"

    status = db.Column(db.String(20), nullable=False, default="IN_PROGRESS", index=True)
    manager_approval = db.Column(db.String(20), nullable=False, default="PENDING", index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    department = db.relationship("Department", foreign_keys=[department_id], lazy="joined")
    work_location = db.relationship("HRLookupItem", foreign_keys=[work_location_lookup_id], lazy="joined")
    entered_by = db.relationship("User", foreign_keys=[entered_by_id], lazy="joined")

    __table_args__ = (
        db.Index("ix_inv_request_date", "request_date"),
    )



# ----------------------
# Inventory: Suppliers / Units / Rooms
# ----------------------

class InvSupplier(db.Model):
    __tablename__ = "inv_supplier"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False, index=True, unique=True)
    phone = db.Column(db.String(80), nullable=True)
    email = db.Column(db.String(120), nullable=True)
    address = db.Column(db.String(255), nullable=True)
    note = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    @property
    def label(self) -> str:
        return (self.name or str(self.id)).strip()


class InvUnit(db.Model):
    __tablename__ = "inv_unit"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, index=True, unique=True)
    note = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    @property
    def label(self) -> str:
        return (self.name or str(self.id)).strip()


class InvRoom(db.Model):
    __tablename__ = "inv_room"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, index=True)
    code = db.Column(db.String(80), nullable=True, index=True)

    unit_id = db.Column(db.Integer, db.ForeignKey("inv_unit.id"), nullable=True, index=True)

    note = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    unit = db.relationship("InvUnit", foreign_keys=[unit_id], lazy="joined")

    __table_args__ = (
        db.Index("ix_inv_room_name_code", "name", "code"),
    )

    @property
    def label(self) -> str:
        base = (self.name or '').strip() or (self.code or '').strip() or str(self.id)
        if self.code and self.code.strip() and self.code.strip() not in base:
            return f"{base} ({self.code.strip()})"
        return base


# ----------------------
# Inventory: Stocktake (Inventory Count) Vouchers
# ----------------------

class InvStocktakeVoucher(db.Model):
    __tablename__ = "inv_stocktake_voucher"

    id = db.Column(db.Integer, primary_key=True)

    voucher_no = db.Column(db.String(80), nullable=False, index=True)
    voucher_date = db.Column(db.String(10), nullable=False, index=True)  # YYYY-MM-DD

    warehouse_id = db.Column(db.Integer, db.ForeignKey("inv_warehouse.id"), nullable=False, index=True)
    note = db.Column(db.Text, nullable=True)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    warehouse = db.relationship("InvWarehouse", foreign_keys=[warehouse_id], lazy="joined")
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")

    __table_args__ = (
        db.Index("ix_inv_stocktake_voucher_date_no", "voucher_date", "voucher_no"),
    )


class InvStocktakeVoucherLine(db.Model):
    __tablename__ = "inv_stocktake_voucher_line"

    id = db.Column(db.Integer, primary_key=True)
    voucher_id = db.Column(db.Integer, db.ForeignKey("inv_stocktake_voucher.id"), nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey("inv_item.id"), nullable=False, index=True)

    qty = db.Column(db.Float, nullable=False, default=0.0)
    serial = db.Column(db.String(200), nullable=True)
    warranty_start = db.Column(db.String(10), nullable=True)  # YYYY-MM-DD
    warranty_end = db.Column(db.String(10), nullable=True)
    details = db.Column(db.Text, nullable=True)

    voucher = db.relationship("InvStocktakeVoucher", foreign_keys=[voucher_id], lazy="joined")
    item = db.relationship("InvItem", foreign_keys=[item_id], lazy="joined")


class InvStocktakeVoucherAttachment(db.Model):
    __tablename__ = "inv_stocktake_voucher_attachment"

    id = db.Column(db.Integer, primary_key=True)
    voucher_id = db.Column(db.Integer, db.ForeignKey("inv_stocktake_voucher.id"), nullable=False, index=True)

    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(500), nullable=False)

    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    voucher = db.relationship("InvStocktakeVoucher", foreign_keys=[voucher_id], lazy="joined")
    uploaded_by = db.relationship("User", foreign_keys=[uploaded_by_id], lazy="joined")


# ----------------------
# Inventory: Custody (عهدة)
# ----------------------

class InvCustodyVoucher(db.Model):
    __tablename__ = "inv_custody_voucher"

    id = db.Column(db.Integer, primary_key=True)

    holder_kind = db.Column(db.String(20), nullable=False, default="EMPLOYEE", index=True)  # EMPLOYEE / ROOM

    voucher_no = db.Column(db.String(80), nullable=False, index=True)
    voucher_date = db.Column(db.String(10), nullable=False, index=True)  # YYYY-MM-DD

    holder_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    holder_room_id = db.Column(db.Integer, db.ForeignKey("inv_room.id"), nullable=True, index=True)

    note = db.Column(db.Text, nullable=True)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    holder_user = db.relationship("User", foreign_keys=[holder_user_id], lazy="joined")
    holder_room = db.relationship("InvRoom", foreign_keys=[holder_room_id], lazy="joined")
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")

    __table_args__ = (
        db.Index("ix_inv_custody_voucher_date_no", "voucher_date", "voucher_no"),
    )


class InvCustodyVoucherLine(db.Model):
    __tablename__ = "inv_custody_voucher_line"

    id = db.Column(db.Integer, primary_key=True)
    voucher_id = db.Column(db.Integer, db.ForeignKey("inv_custody_voucher.id"), nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey("inv_item.id"), nullable=False, index=True)

    qty = db.Column(db.Float, nullable=False, default=1.0)
    serial = db.Column(db.String(200), nullable=True)
    warranty_start = db.Column(db.String(10), nullable=True)  # YYYY-MM-DD
    warranty_end = db.Column(db.String(10), nullable=True)
    details = db.Column(db.Text, nullable=True)

    voucher = db.relationship("InvCustodyVoucher", foreign_keys=[voucher_id], lazy="joined")
    item = db.relationship("InvItem", foreign_keys=[item_id], lazy="joined")


class InvCustodyVoucherAttachment(db.Model):
    __tablename__ = "inv_custody_voucher_attachment"

    id = db.Column(db.Integer, primary_key=True)
    voucher_id = db.Column(db.Integer, db.ForeignKey("inv_custody_voucher.id"), nullable=False, index=True)

    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(500), nullable=False)

    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    voucher = db.relationship("InvCustodyVoucher", foreign_keys=[voucher_id], lazy="joined")
    uploaded_by = db.relationship("User", foreign_keys=[uploaded_by_id], lazy="joined")


# ----------------------
# Inventory: Control panel mappings
# ----------------------

class InvRoomRequester(db.Model):
    __tablename__ = "inv_room_requester"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    room_id = db.Column(db.Integer, db.ForeignKey("inv_room.id"), nullable=True, index=True)

    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User", foreign_keys=[user_id], lazy="joined")
    room = db.relationship("InvRoom", foreign_keys=[room_id], lazy="joined")

    __table_args__ = (
        db.Index("ix_inv_room_requester_user_room", "user_id", "room_id"),
    )


class InvWarehousePermission(db.Model):
    __tablename__ = "inv_warehouse_permission"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("inv_warehouse.id"), nullable=False, index=True)

    can_view = db.Column(db.Boolean, default=True, nullable=False)
    can_issue = db.Column(db.Boolean, default=False, nullable=False)
    can_inbound = db.Column(db.Boolean, default=False, nullable=False)
    can_stocktake = db.Column(db.Boolean, default=False, nullable=False)
    can_manage = db.Column(db.Boolean, default=False, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User", foreign_keys=[user_id], lazy="joined")
    warehouse = db.relationship("InvWarehouse", foreign_keys=[warehouse_id], lazy="joined")

    __table_args__ = (
        db.UniqueConstraint("user_id", "warehouse_id", name="uq_inv_wh_perm_user_wh"),
    )

# ======================
# Portal: Access Requests (Request permission within Portal)
# ======================
class PortalAccessRequest(db.Model):
    """A lightweight workflow for employees to request portal permissions.

    - Created by an employee from the Portal home cards (disabled modules).
    - Reviewed by Portal Admin (PORTAL_ADMIN_PERMISSIONS_MANAGE).
    - When approved, requested permission keys are granted as UserPermission.
    """

    __tablename__ = "portal_access_request"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    # Service identifier: corr / attendance / hr / store
    service = db.Column(db.String(40), nullable=False, index=True)

    # Comma-separated permission keys (uppercased)
    requested_keys = db.Column(db.String(800), nullable=False)

    note = db.Column(db.Text, nullable=True)

    # PENDING / APPROVED / REJECTED / CANCELLED
    status = db.Column(db.String(20), nullable=False, default="PENDING", index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    decided_at = db.Column(db.DateTime, nullable=True)
    decided_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    decision_note = db.Column(db.Text, nullable=True)

    # Optional routing/assignment (helps distribute review load)
    assigned_to_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    assigned_role = db.Column(db.String(50), nullable=True, index=True)

    user = db.relationship("User", foreign_keys=[user_id], lazy="joined")
    decided_by = db.relationship("User", foreign_keys=[decided_by_id], lazy="joined")
    assigned_to = db.relationship("User", foreign_keys=[assigned_to_user_id], lazy="joined")

    __table_args__ = (
        db.Index("ix_portal_access_req_user_status", "user_id", "status"),
        db.Index("ix_portal_access_req_service_status", "service", "status"),
        db.Index("ix_portal_access_req_assigned_status", "assigned_to_user_id", "status"),
    )

    @property
    def keys_list(self) -> list[str]:
        raw = (self.requested_keys or "").strip()
        if not raw:
            return []
        return [k.strip().upper() for k in raw.split(",") if k.strip()]







# ======================
# Portal: Circulars (التعميمات)
# ======================
class PortalCircular(db.Model):
    __tablename__ = "portal_circulars"

    __table_args__ = (
        db.Index("ix_portal_circulars_created", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    is_urgent = db.Column(db.Boolean, default=True, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    created_by = db.relationship("User", foreign_keys=[created_by_user_id], lazy="joined")
# ======================
# Portal: Saved Filters
# ======================
class SavedFilter(db.Model):
    """User-saved filters (query presets) for portal lists."""

    __tablename__ = "saved_filter"

    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    # Scope is a short module key, e.g. "HR", "CORR", "STORE"
    scope = db.Column(db.String(40), nullable=False, index=True)

    name = db.Column(db.String(120), nullable=False)

    # Local path within the app (must start with "/")
    path = db.Column(db.String(300), nullable=False)

    # Stored query string without leading '?'
    query_string = db.Column(db.String(4000), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    owner = db.relationship("User", foreign_keys=[owner_id], lazy="joined")

    __table_args__ = (
        db.UniqueConstraint("owner_id", "scope", "name", name="uq_saved_filter_owner_scope_name"),
        db.Index("ix_saved_filter_owner_scope", "owner_id", "scope"),
    )


# ======================
# Portal: Correspondence (Inbound / Outbound)
# ======================
class CorrCategory(db.Model):
    """Lookup table for correspondence categories."""

    __tablename__ = "corr_category"

    id = db.Column(db.Integer, primary_key=True)

    code = db.Column(db.String(50), nullable=False, unique=True, index=True)
    name_ar = db.Column(db.String(200), nullable=False)
    name_en = db.Column(db.String(200), nullable=True)

    is_active = db.Column(db.Boolean, default=True, nullable=False, index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
    @property
    def label(self) -> str:
        return (self.name_ar or self.name_en or self.code or "").strip() or self.code


class CorrParty(db.Model):
    """Lookup table for correspondence parties (senders/recipients)."""

    __tablename__ = "corr_party"

    id = db.Column(db.Integer, primary_key=True)

    # SENDER / RECIPIENT / BOTH
    kind = db.Column(db.String(20), nullable=False, index=True)

    name_ar = db.Column(db.String(200), nullable=False)
    name_en = db.Column(db.String(200), nullable=True)

    is_active = db.Column(db.Boolean, default=True, nullable=False, index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
    __table_args__ = (
        db.CheckConstraint("kind IN ('SENDER','RECIPIENT','BOTH')", name="ck_corr_party_kind"),
        db.Index("ix_corr_party_active_kind", "is_active", "kind"),
    )

    @property
    def label(self) -> str:
        return (self.name_ar or self.name_en or "").strip() or (self.name_en or "")


class InboundMail(db.Model):
    """Inbound correspondence register item."""

    __tablename__ = "corr_inbound"

    id = db.Column(db.Integer, primary_key=True)

    ref_no = db.Column(db.String(50), nullable=True, index=True)

    # Store category code (from CorrCategory.code) to keep the register independent.
    category = db.Column(db.String(50), nullable=False, default="GENERAL", index=True)

    sender = db.Column(db.String(200), nullable=True, index=True)

    subject = db.Column(db.String(500), nullable=False)
    body = db.Column(db.Text, nullable=True)

    # Keep as string YYYY-MM-DD (matches portal forms & filtering)
    received_date = db.Column(db.String(10), nullable=False, index=True)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
    def __repr__(self) -> str:
        return f"<InboundMail id={self.id} ref={self.ref_no!r}>"


class OutboundMail(db.Model):
    """Outbound correspondence register item."""

    __tablename__ = "corr_outbound"

    id = db.Column(db.Integer, primary_key=True)

    ref_no = db.Column(db.String(50), nullable=True, index=True)

    category = db.Column(db.String(50), nullable=False, default="GENERAL", index=True)

    recipient = db.Column(db.String(200), nullable=True, index=True)

    subject = db.Column(db.String(500), nullable=False)
    body = db.Column(db.Text, nullable=True)

    # Keep as string YYYY-MM-DD
    sent_date = db.Column(db.String(10), nullable=False, index=True)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
    def __repr__(self) -> str:
        return f"<OutboundMail id={self.id} ref={self.ref_no!r}>"


class CorrAttachment(db.Model):
    """File attachment linked to inbound/outbound correspondence."""

    __tablename__ = "corr_attachment"

    id = db.Column(db.Integer, primary_key=True)

    inbound_id = db.Column(db.Integer, db.ForeignKey("corr_inbound.id"), nullable=True, index=True)
    outbound_id = db.Column(db.Integer, db.ForeignKey("corr_outbound.id"), nullable=True, index=True)

    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False)

    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    published_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    uploaded_by = db.relationship("User", foreign_keys=[uploaded_by_id], lazy="joined")
    published_by = db.relationship("User", foreign_keys=[published_by_id], lazy="joined")

    inbound = db.relationship(
        "InboundMail",
        foreign_keys=[inbound_id],
        backref=db.backref("attachments", lazy="dynamic", cascade="all, delete-orphan"),
        lazy="joined",
    )
    outbound = db.relationship(
        "OutboundMail",
        foreign_keys=[outbound_id],
        backref=db.backref("attachments", lazy="dynamic", cascade="all, delete-orphan"),
        lazy="joined",
    )

    __table_args__ = (
        # At least one of inbound_id/outbound_id must be set.
        db.CheckConstraint(
            "(inbound_id IS NOT NULL) OR (outbound_id IS NOT NULL)",
            name="ck_corr_attachment_parent",
        ),
        db.Index("ix_corr_attachment_inbound", "inbound_id"),
        db.Index("ix_corr_attachment_outbound", "outbound_id"),
    )


class CorrCounter(db.Model):
    """Reference number counter for correspondence.

    Partitioned by kind (IN/OUT), year, and category.
    """

    __tablename__ = "corr_counter"

    id = db.Column(db.Integer, primary_key=True)

    kind = db.Column(db.String(5), nullable=False, index=True)  # IN / OUT
    year = db.Column(db.Integer, nullable=False, index=True)
    category = db.Column(db.String(50), nullable=False, index=True)

    last_no = db.Column(db.Integer, default=0, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("kind", "year", "category", name="uq_corr_counter_kind_year_cat"),
        db.Index("ix_corr_counter_kind_year", "kind", "year"),
    )


# =========================================================
# Portal HR: Performance & Evaluation (360)
# =========================================================

class HRPerformanceForm(db.Model):
    """Dynamic performance evaluation form (template).

    Admin can create forms with sections + questions.
    Used by cycles.
    """
    __tablename__ = 'hr_perf_form'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)

    is_active = db.Column(db.Boolean, default=True, nullable=False, index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    created_by = db.relationship('User', foreign_keys=[created_by_id], lazy='joined')


class HRPerformanceSection(db.Model):
    __tablename__ = 'hr_perf_section'

    id = db.Column(db.Integer, primary_key=True)
    form_id = db.Column(db.Integer, db.ForeignKey('hr_perf_form.id'), nullable=False, index=True)

    title = db.Column(db.String(200), nullable=False)
    order_no = db.Column(db.Integer, default=0, nullable=False)

    form = db.relationship('HRPerformanceForm', backref=db.backref('sections', lazy='selectin'))


class HRPerformanceQuestion(db.Model):
    """Question definition.

    q_type:
      - RATING_1_5: integer 1..5
      - TEXT: free text
      - YESNO: yes/no
      - NUMBER: numeric
    """
    __tablename__ = 'hr_perf_question'

    id = db.Column(db.Integer, primary_key=True)
    section_id = db.Column(db.Integer, db.ForeignKey('hr_perf_section.id'), nullable=False, index=True)

    prompt = db.Column(db.String(500), nullable=False)
    q_type = db.Column(db.String(20), nullable=False, default='RATING_1_5')
    is_required = db.Column(db.Boolean, default=False, nullable=False)

    weight = db.Column(db.Float, default=1.0, nullable=False)
    order_no = db.Column(db.Integer, default=0, nullable=False)

    section = db.relationship('HRPerformanceSection', backref=db.backref('questions', lazy='selectin'))

    __table_args__ = (
        db.CheckConstraint("q_type IN ('RATING_1_5','TEXT','YESNO','NUMBER')", name='ck_hr_perf_q_type'),
    )


class HRPerformanceCycle(db.Model):
    """Evaluation cycle based on a form.

    status: DRAFT / ACTIVE / CLOSED
    peer_count: how many peers to assign automatically (best-effort)
    """
    __tablename__ = 'hr_perf_cycle'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)

    form_id = db.Column(db.Integer, db.ForeignKey('hr_perf_form.id'), nullable=False, index=True)

    start_date = db.Column(db.String(10), nullable=True, index=True)  # YYYY-MM-DD
    end_date = db.Column(db.String(10), nullable=True, index=True)

    status = db.Column(db.String(20), nullable=False, default='DRAFT', index=True)

    peer_count = db.Column(db.Integer, default=2, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    form = db.relationship('HRPerformanceForm', foreign_keys=[form_id], lazy='joined')
    created_by = db.relationship('User', foreign_keys=[created_by_id], lazy='joined')

    __table_args__ = (
        db.CheckConstraint("status IN ('DRAFT','ACTIVE','CLOSED')", name='ck_hr_perf_cycle_status'),
    )


class HRPerformanceAssignment(db.Model):
    """An assignment from evaluator -> evaluatee within a cycle."""
    __tablename__ = 'hr_perf_assignment'

    id = db.Column(db.Integer, primary_key=True)

    cycle_id = db.Column(db.Integer, db.ForeignKey('hr_perf_cycle.id'), nullable=False, index=True)

    evaluatee_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    evaluator_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)

    evaluator_type = db.Column(db.String(20), nullable=False, default='PEER')  # SELF / MANAGER / PEER

    status = db.Column(db.String(20), nullable=False, default='PENDING', index=True)
    due_date = db.Column(db.String(10), nullable=True, index=True)

    submitted_at = db.Column(db.DateTime, nullable=True)

    # JSON payload: { question_id: {"value": ..., "comment": ...}, "overall_comment": "..." }
    answers_json = db.Column(db.Text, nullable=True)

    score_total = db.Column(db.Float, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    cycle = db.relationship('HRPerformanceCycle', foreign_keys=[cycle_id], lazy='joined')
    evaluatee = db.relationship('User', foreign_keys=[evaluatee_user_id], lazy='joined')
    evaluator = db.relationship('User', foreign_keys=[evaluator_user_id], lazy='joined')
    created_by = db.relationship('User', foreign_keys=[created_by_id], lazy='joined')

    __table_args__ = (
        db.UniqueConstraint('cycle_id','evaluatee_user_id','evaluator_user_id','evaluator_type', name='uq_hr_perf_assign_unique'),
        db.CheckConstraint("evaluator_type IN ('SELF','MANAGER','PEER')", name='ck_hr_perf_assign_type'),
        db.CheckConstraint("status IN ('PENDING','SUBMITTED')", name='ck_hr_perf_assign_status'),
        db.Index('ix_hr_perf_assign_cycle_eval', 'cycle_id', 'evaluator_user_id'),
        db.Index('ix_hr_perf_assign_cycle_ee', 'cycle_id', 'evaluatee_user_id'),
    )
# Transport / Fleet Models (Portal)
# ======================
class TransportVehicle(db.Model):
    __tablename__ = "transport_vehicle"

    id = db.Column(db.Integer, primary_key=True)

    plate_no = db.Column(db.String(50), nullable=False, unique=True, index=True)
    label = db.Column(db.String(120), nullable=True)  # اسم/وصف المركبة
    vehicle_type = db.Column(db.String(30), nullable=True)  # CAR / VAN / BUS / ...
    model = db.Column(db.String(120), nullable=True)
    year = db.Column(db.Integer, nullable=True)

    status = db.Column(db.String(20), nullable=False, default="ACTIVE", index=True)  # ACTIVE / INACTIVE / MAINTENANCE

    current_odometer = db.Column(db.Float, nullable=False, default=0.0)



    # Fleet fields (requirements)
    manufacture_day = db.Column(db.String(10), nullable=True, index=True)  # YYYY-MM-DD
    fuel_card_no = db.Column(db.String(80), nullable=True, index=True)
    fuel_type_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True, index=True)
    service_start_day = db.Column(db.String(10), nullable=True, index=True)
    license_end_day = db.Column(db.String(10), nullable=True, index=True)
    insurance_end_day = db.Column(db.String(10), nullable=True, index=True)
    work_location_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True, index=True)

    consumption_rate = db.Column(db.Float, nullable=True)  # معدل الاستهلاك
    max_fuel_limit = db.Column(db.Float, nullable=True)    # الحد الأقصى للوقود
    # Tracking (Option C)
    tracking_device_uid = db.Column(db.String(120), nullable=True, index=True)  # IMEI / UnitId / TrackerId
    tracking_enabled = db.Column(db.Boolean, nullable=False, default=False)

    notes = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
    fuel_type_lookup = db.relationship("HRLookupItem", foreign_keys=[fuel_type_lookup_id], lazy="joined")
    work_location_lookup = db.relationship("HRLookupItem", foreign_keys=[work_location_lookup_id], lazy="joined")
    __table_args__ = (
        db.CheckConstraint("status IN ('ACTIVE','INACTIVE','MAINTENANCE')", name="ck_transport_vehicle_status"),
    )


class TransportDriver(db.Model):
    __tablename__ = "transport_driver"

    id = db.Column(db.Integer, primary_key=True)

    # Optional link to system user (portal user)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    name = db.Column(db.String(200), nullable=False, index=True)
    phone = db.Column(db.String(50), nullable=True)
    license_no = db.Column(db.String(100), nullable=True)

    status = db.Column(db.String(20), nullable=False, default="ACTIVE", index=True)  # ACTIVE / INACTIVE

    notes = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    user = db.relationship("User", foreign_keys=[user_id], lazy="joined")
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
    __table_args__ = (
        db.CheckConstraint("status IN ('ACTIVE','INACTIVE')", name="ck_transport_driver_status"),
    )


class TransportZone(db.Model):
    __tablename__ = "transport_zone"

    id = db.Column(db.Integer, primary_key=True)

    name = db.Column(db.String(200), nullable=False, unique=True, index=True)
    city = db.Column(db.String(120), nullable=True)

    is_active = db.Column(db.Boolean, nullable=False, default=True)

    notes = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
class TransportPermit(db.Model):
    __tablename__ = "transport_permit"

    id = db.Column(db.Integer, primary_key=True)

    ref_no = db.Column(db.String(50), nullable=True, index=True)  # optional human ref

    requester_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    vehicle_id = db.Column(db.Integer, db.ForeignKey("transport_vehicle.id"), nullable=True, index=True)
    driver_id = db.Column(db.Integer, db.ForeignKey("transport_driver.id"), nullable=True, index=True)

    origin_zone_id = db.Column(db.Integer, db.ForeignKey("transport_zone.id"), nullable=True, index=True)
    dest_zone_id = db.Column(db.Integer, db.ForeignKey("transport_zone.id"), nullable=True, index=True)

    origin_text = db.Column(db.String(255), nullable=True)
    dest_text = db.Column(db.String(255), nullable=True)

    purpose = db.Column(db.String(255), nullable=False)
    passengers_count = db.Column(db.Integer, nullable=True)

    depart_at = db.Column(db.DateTime, nullable=True, index=True)
    return_at = db.Column(db.DateTime, nullable=True, index=True)

    status = db.Column(db.String(20), nullable=False, default="DRAFT", index=True)  # DRAFT/SUBMITTED/APPROVED/REJECTED/CANCELLED/COMPLETED

    note = db.Column(db.Text, nullable=True)

    submitted_at = db.Column(db.DateTime, nullable=True)
    decided_at = db.Column(db.DateTime, nullable=True)

    approver_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    decision_note = db.Column(db.Text, nullable=True)

    # Soft delete
    is_deleted = db.Column(db.Boolean, nullable=False, default=False, index=True)
    deleted_at = db.Column(db.DateTime, nullable=True)
    deleted_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    requester = db.relationship("User", foreign_keys=[requester_user_id], lazy="joined")
    approver = db.relationship("User", foreign_keys=[approver_user_id], lazy="joined")
    deleted_by = db.relationship("User", foreign_keys=[deleted_by_id], lazy="joined")

    vehicle = db.relationship("TransportVehicle", foreign_keys=[vehicle_id], lazy="joined")
    driver = db.relationship("TransportDriver", foreign_keys=[driver_id], lazy="joined")

    origin_zone = db.relationship("TransportZone", foreign_keys=[origin_zone_id], lazy="joined")
    dest_zone = db.relationship("TransportZone", foreign_keys=[dest_zone_id], lazy="joined")

    __table_args__ = (
        db.CheckConstraint("status IN ('DRAFT','SUBMITTED','APPROVED','REJECTED','CANCELLED','COMPLETED')", name="ck_transport_permit_status"),
        db.Index("ix_transport_permit_status_date", "status", "depart_at"),
    )


class TransportTrip(db.Model):
    __tablename__ = "transport_trip"

    id = db.Column(db.Integer, primary_key=True)

    permit_id = db.Column(db.Integer, db.ForeignKey("transport_permit.id"), nullable=True, index=True)

    vehicle_id = db.Column(db.Integer, db.ForeignKey("transport_vehicle.id"), nullable=False, index=True)
    driver_id = db.Column(db.Integer, db.ForeignKey("transport_driver.id"), nullable=True, index=True)

    started_at = db.Column(db.DateTime, nullable=False, index=True)
    ended_at = db.Column(db.DateTime, nullable=True, index=True)

    start_odometer = db.Column(db.Float, nullable=True)
    end_odometer = db.Column(db.Float, nullable=True)
    distance_km = db.Column(db.Float, nullable=True)

    note = db.Column(db.Text, nullable=True)

    order_no = db.Column(db.String(50), nullable=True, index=True)
    place_kind = db.Column(db.String(20), nullable=True, index=True)  # INTERNAL / EXTERNAL

    # Soft delete
    is_deleted = db.Column(db.Boolean, nullable=False, default=False, index=True)
    deleted_at = db.Column(db.DateTime, nullable=True)
    deleted_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    permit = db.relationship("TransportPermit", foreign_keys=[permit_id], lazy="joined")
    vehicle = db.relationship("TransportVehicle", foreign_keys=[vehicle_id], lazy="joined")
    driver = db.relationship("TransportDriver", foreign_keys=[driver_id], lazy="joined")
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
    deleted_by = db.relationship("User", foreign_keys=[deleted_by_id], lazy="joined")
    __table_args__ = (
        db.Index("ix_transport_trip_vehicle_date", "vehicle_id", "started_at"),
    )


class TransportTripPoint(db.Model):
    __tablename__ = "transport_trip_point"

    id = db.Column(db.Integer, primary_key=True)

    trip_id = db.Column(db.Integer, db.ForeignKey("transport_trip.id"), nullable=False, index=True)

    seq = db.Column(db.Integer, nullable=False, default=0)

    recorded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    lat = db.Column(db.Float, nullable=True)
    lng = db.Column(db.Float, nullable=True)
    speed = db.Column(db.Float, nullable=True)
    heading = db.Column(db.Float, nullable=True)

    raw_json = db.Column(db.Text, nullable=True)

    trip = db.relationship("TransportTrip", foreign_keys=[trip_id], lazy="joined")

    __table_args__ = (
        db.Index("ix_transport_trip_point_trip_seq", "trip_id", "seq"),
    )


class TransportDriverTask(db.Model):
    __tablename__ = "transport_driver_task"

    id = db.Column(db.Integer, primary_key=True)

    driver_id = db.Column(db.Integer, db.ForeignKey("transport_driver.id"), nullable=False, index=True)
    permit_id = db.Column(db.Integer, db.ForeignKey("transport_permit.id"), nullable=True, index=True)
    trip_id = db.Column(db.Integer, db.ForeignKey("transport_trip.id"), nullable=True, index=True)

    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=True)

    due_date = db.Column(db.String(10), nullable=True, index=True)  # YYYY-MM-DD
    status = db.Column(db.String(20), nullable=False, default="PENDING", index=True)  # PENDING/IN_PROGRESS/DONE/CANCELLED

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    completed_at = db.Column(db.DateTime, nullable=True)

    driver = db.relationship("TransportDriver", foreign_keys=[driver_id], lazy="joined")
    permit = db.relationship("TransportPermit", foreign_keys=[permit_id], lazy="joined")
    trip = db.relationship("TransportTrip", foreign_keys=[trip_id], lazy="joined")
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
    __table_args__ = (
        db.CheckConstraint("status IN ('PENDING','IN_PROGRESS','DONE','CANCELLED')", name="ck_transport_task_status"),
    )






# -------------------------
# Transport Extensions (Phase 1 - DB only)
# -------------------------
class TransportDestination(db.Model):
    __tablename__ = "transport_destination"
    id = db.Column(db.Integer, primary_key=True)

    name = db.Column(db.String(255), nullable=False, unique=True, index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    notes = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")


class TransportTripDestination(db.Model):
    __tablename__ = "transport_trip_destination"
    id = db.Column(db.Integer, primary_key=True)

    trip_id = db.Column(db.Integer, db.ForeignKey("transport_trip.id"), nullable=False, index=True)
    destination_id = db.Column(db.Integer, db.ForeignKey("transport_destination.id"), nullable=False, index=True)

    seq = db.Column(db.Integer, nullable=False, default=0)
    note = db.Column(db.Text, nullable=True)

    trip = db.relationship("TransportTrip", foreign_keys=[trip_id], lazy="joined")
    destination = db.relationship("TransportDestination", foreign_keys=[destination_id], lazy="joined")

    __table_args__ = (
        db.Index("ix_transport_trip_dest_trip_seq", "trip_id", "seq"),
        db.UniqueConstraint("trip_id", "destination_id", name="ux_transport_trip_dest_unique"),
    )


class TransportVehicleAttachment(db.Model):
    __tablename__ = "transport_vehicle_attachment"
    id = db.Column(db.Integer, primary_key=True)

    vehicle_id = db.Column(db.Integer, db.ForeignKey("transport_vehicle.id"), nullable=False, index=True)

    original_name = db.Column(db.String(255), nullable=True)
    stored_name = db.Column(db.String(255), nullable=True, index=True)
    content_type = db.Column(db.String(120), nullable=True)
    size_bytes = db.Column(db.Integer, nullable=True)

    attachment_type_lookup_id = db.Column(db.Integer, db.ForeignKey("hr_lookup_item.id"), nullable=True, index=True)
    note = db.Column(db.Text, nullable=True)

    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    vehicle = db.relationship("TransportVehicle", foreign_keys=[vehicle_id], lazy="joined")
    uploaded_by = db.relationship("User", foreign_keys=[uploaded_by_id], lazy="joined")
    attachment_type_lookup = db.relationship("HRLookupItem", foreign_keys=[attachment_type_lookup_id], lazy="joined")


class TransportMaintenance(db.Model):
    __tablename__ = "transport_maintenance"
    id = db.Column(db.Integer, primary_key=True)

    vehicle_id = db.Column(db.Integer, db.ForeignKey("transport_vehicle.id"), nullable=False, index=True)

    invoice_no = db.Column(db.String(120), nullable=True, index=True)
    invoice_day = db.Column(db.String(10), nullable=True, index=True)  # YYYY-MM-DD

    garage_lookup_id = db.Column(db.Integer, db.ForeignKey("hr_lookup_item.id"), nullable=True, index=True)  # الكراج/الشركة
    notes = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    vehicle = db.relationship("TransportVehicle", foreign_keys=[vehicle_id], lazy="joined")
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
    garage_lookup = db.relationship("HRLookupItem", foreign_keys=[garage_lookup_id], lazy="joined")


class TransportMaintenanceItem(db.Model):
    __tablename__ = "transport_maintenance_item"
    id = db.Column(db.Integer, primary_key=True)

    maintenance_id = db.Column(db.Integer, db.ForeignKey("transport_maintenance.id"), nullable=False, index=True)

    maintenance_type_lookup_id = db.Column(db.Integer, db.ForeignKey("hr_lookup_item.id"), nullable=True, index=True)
    quantity = db.Column(db.Float, nullable=True)
    unit_price = db.Column(db.Float, nullable=True)
    total_price = db.Column(db.Float, nullable=True)

    note = db.Column(db.Text, nullable=True)

    maintenance = db.relationship("TransportMaintenance", foreign_keys=[maintenance_id], lazy="joined")
    maintenance_type_lookup = db.relationship("HRLookupItem", foreign_keys=[maintenance_type_lookup_id], lazy="joined")

    __table_args__ = (
        db.Index("ix_transport_maint_item_maint", "maintenance_id"),
    )


class TransportMaintenanceAttachment(db.Model):
    __tablename__ = "transport_maintenance_attachment"
    id = db.Column(db.Integer, primary_key=True)

    maintenance_id = db.Column(db.Integer, db.ForeignKey("transport_maintenance.id"), nullable=False, index=True)

    original_name = db.Column(db.String(255), nullable=True)
    stored_name = db.Column(db.String(255), nullable=True, index=True)
    content_type = db.Column(db.String(120), nullable=True)
    size_bytes = db.Column(db.Integer, nullable=True)

    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    maintenance = db.relationship("TransportMaintenance", foreign_keys=[maintenance_id], lazy="joined")
    uploaded_by = db.relationship("User", foreign_keys=[uploaded_by_id], lazy="joined")


class TransportFuelFill(db.Model):
    __tablename__ = "transport_fuel_fill"
    id = db.Column(db.Integer, primary_key=True)

    vehicle_id = db.Column(db.Integer, db.ForeignKey("transport_vehicle.id"), nullable=False, index=True)

    fill_day = db.Column(db.String(10), nullable=True, index=True)  # YYYY-MM-DD
    payment_method_lookup_id = db.Column(db.Integer, db.ForeignKey("hr_lookup_item.id"), nullable=True, index=True)
    invoice_no = db.Column(db.String(120), nullable=True, index=True)

    liters = db.Column(db.Float, nullable=True)
    amount = db.Column(db.Float, nullable=True)

    station_lookup_id = db.Column(db.Integer, db.ForeignKey("hr_lookup_item.id"), nullable=True, index=True)  # محطة الوقود
    notes = db.Column(db.Text, nullable=True)

    odometer_value = db.Column(db.Float, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    vehicle = db.relationship("TransportVehicle", foreign_keys=[vehicle_id], lazy="joined")
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")
    payment_method_lookup = db.relationship("HRLookupItem", foreign_keys=[payment_method_lookup_id], lazy="joined")
    station_lookup = db.relationship("HRLookupItem", foreign_keys=[station_lookup_id], lazy="joined")


class TransportFuelFillAttachment(db.Model):
    __tablename__ = "transport_fuel_fill_attachment"
    id = db.Column(db.Integer, primary_key=True)

    fuel_fill_id = db.Column(db.Integer, db.ForeignKey("transport_fuel_fill.id"), nullable=False, index=True)

    original_name = db.Column(db.String(255), nullable=True)
    stored_name = db.Column(db.String(255), nullable=True, index=True)
    content_type = db.Column(db.String(120), nullable=True)
    size_bytes = db.Column(db.Integer, nullable=True)

    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    fuel_fill = db.relationship("TransportFuelFill", foreign_keys=[fuel_fill_id], lazy="joined")
    uploaded_by = db.relationship("User", foreign_keys=[uploaded_by_id], lazy="joined")



# HR_LEAVES_MISSIONS_EVENTS_PATCH_V1

class HRStatusDef(db.Model):
    __tablename__ = "hr_status_def"
    id = db.Column(db.Integer, primary_key=True)
    entity = db.Column(db.String(20), nullable=False, index=True)  # LEAVE / MISSION
    code = db.Column(db.String(50), nullable=False, index=True)
    name_ar = db.Column(db.String(200), nullable=False)
    name_en = db.Column(db.String(200), nullable=True)
    sort_order = db.Column(db.Integer, nullable=False, default=100)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")

    __table_args__ = (
        db.Index("ux_hr_status_def_entity_code", "entity", "code", unique=True),
    )


class HROfficialMissionAttachment(db.Model):
    __tablename__ = "hr_official_mission_attachment"
    id = db.Column(db.Integer, primary_key=True)
    mission_id = db.Column(db.Integer, db.ForeignKey("hr_official_mission.id"), nullable=False, index=True)

    original_name = db.Column(db.String(255), nullable=True)
    stored_name = db.Column(db.String(255), nullable=True)
    content_type = db.Column(db.String(120), nullable=True)
    size_bytes = db.Column(db.Integer, nullable=True)

    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    mission = db.relationship("HROfficialMission", back_populates="attachments", lazy="joined")
    uploaded_by = db.relationship("User", foreign_keys=[uploaded_by_id], lazy="joined")


class HROfficialOccasionType(db.Model):
    __tablename__ = "hr_official_occasion_type"
    id = db.Column(db.Integer, primary_key=True)
    name_ar = db.Column(db.String(200), nullable=False)
    name_en = db.Column(db.String(200), nullable=True)
    is_day_off_default = db.Column(db.Boolean, default=True, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=100)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")


class HROfficialOccasionRange(db.Model):
    __tablename__ = "hr_official_occasion_range"
    id = db.Column(db.Integer, primary_key=True)

    work_governorate_lookup_id = db.Column(db.Integer, db.ForeignKey("hr_lookup_item.id"), nullable=True, index=True)
    work_location_lookup_id = db.Column(db.Integer, db.ForeignKey("hr_lookup_item.id"), nullable=True, index=True)

    type_id = db.Column(db.Integer, db.ForeignKey("hr_official_occasion_type.id"), nullable=False, index=True)

    title = db.Column(db.String(200), nullable=False, default="")
    note = db.Column(db.Text, nullable=True)

    start_day = db.Column(db.String(10), nullable=False, index=True)  # YYYY-MM-DD
    end_day = db.Column(db.String(10), nullable=False, index=True)

    is_day_off = db.Column(db.Boolean, default=True, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    type = db.relationship("HROfficialOccasionType", foreign_keys=[type_id], lazy="joined")
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")

    work_governorate_lookup = db.relationship("HRLookupItem", foreign_keys=[work_governorate_lookup_id], lazy="joined")
    work_location_lookup = db.relationship("HRLookupItem", foreign_keys=[work_location_lookup_id], lazy="joined")


# ======================
# HR Training (التدريب)
# ======================

class HRTrainingCourse(db.Model):
    """Course list item under a category (تصنيف الدورات -> تفاصيل الدورة)."""

    __tablename__ = "hr_training_course"

    id = db.Column(db.Integer, primary_key=True)

    # Lookup category item from HRLookupItem(category='TRAINING_CATEGORY')
    category_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True, index=True)

    name_ar = db.Column(db.String(255), nullable=False, default="")
    name_en = db.Column(db.String(255), nullable=True)

    is_active = db.Column(db.Boolean, default=True, nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=100)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)

    category_lookup = db.relationship('HRLookupItem', foreign_keys=[category_lookup_id], lazy='joined')
    created_by = db.relationship('User', foreign_keys=[created_by_id], lazy='joined')


class HRTrainingProgram(db.Model):
    """Training program instance (تدريب مُدخل)."""

    __tablename__ = "hr_training_program"

    id = db.Column(db.Integer, primary_key=True)

    program_no = db.Column(db.String(50), nullable=False, default="", index=True)

    course_id = db.Column(db.Integer, db.ForeignKey('hr_training_course.id'), nullable=True, index=True)

    start_date = db.Column(db.String(10), nullable=True, index=True)  # YYYY-MM-DD
    end_date = db.Column(db.String(10), nullable=True, index=True)    # YYYY-MM-DD

    country_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True, index=True)  # category=COUNTRY
    venue = db.Column(db.String(255), nullable=True)  # مكان الانعقاد

    sponsor_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True, index=True)  # category=TRAINING_SPONSOR

    hours = db.Column(db.Integer, nullable=True)

    is_hosted = db.Column(db.Boolean, default=False, nullable=False)

    trainer_name = db.Column(db.String(200), nullable=True)

    notes = db.Column(db.Text, nullable=True)

    # Circular settings
    is_published = db.Column(db.Boolean, default=False, nullable=False, index=True)
    # نشر حسب الشروط فقط: يظهر/يُعمَّم فقط لمن تنطبق عليهم الشروط
    publish_conditions_only = db.Column(db.Boolean, default=False, nullable=False, index=True)
    nomination_start = db.Column(db.String(10), nullable=True, index=True)
    nomination_end = db.Column(db.String(10), nullable=True, index=True)

    # Per-training overrides (None => use system settings)
    require_manager_approval = db.Column(db.Boolean, nullable=True)
    needs_training_needs_window = db.Column(db.Boolean, nullable=True)  # نافذة إدخال الاحتياجات التدريبية
    employee_notifications_enabled = db.Column(db.Boolean, nullable=True)
    apply_conditions_on_portal = db.Column(db.Boolean, nullable=True)
    apply_conditions_in_program = db.Column(db.Boolean, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    created_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    updated_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)

    course = db.relationship('HRTrainingCourse', foreign_keys=[course_id], lazy='joined')

    country_lookup = db.relationship('HRLookupItem', foreign_keys=[country_lookup_id], lazy='joined')
    sponsor_lookup = db.relationship('HRLookupItem', foreign_keys=[sponsor_lookup_id], lazy='joined')

    created_by = db.relationship('User', foreign_keys=[created_by_id], lazy='joined')
    updated_by = db.relationship('User', foreign_keys=[updated_by_id], lazy='joined')

    conditions = db.relationship('HRTrainingCondition', back_populates='program', cascade='all, delete-orphan', lazy='selectin')
    attachments = db.relationship('HRTrainingAttachment', back_populates='program', cascade='all, delete-orphan', lazy='selectin')
    enrollments = db.relationship('HRTrainingEnrollment', back_populates='program', cascade='all, delete-orphan', lazy='selectin')


class HRTrainingCondition(db.Model):
    __tablename__ = "hr_training_condition"

    id = db.Column(db.Integer, primary_key=True)

    program_id = db.Column(db.Integer, db.ForeignKey('hr_training_program.id'), nullable=False, index=True)

    # Lookup item from HRLookupItem(category='TRAINING_CONDITION_FIELD')
    field_lookup_id = db.Column(db.Integer, db.ForeignKey('hr_lookup_item.id'), nullable=True, index=True)

    operator = db.Column(db.String(20), nullable=False, default="EQ")  # EQ/GT/LT/GTE/LTE/NEQ/BETWEEN
    value1 = db.Column(db.String(255), nullable=True)
    value2 = db.Column(db.String(255), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    program = db.relationship('HRTrainingProgram', back_populates='conditions', lazy='joined')
    field_lookup = db.relationship('HRLookupItem', foreign_keys=[field_lookup_id], lazy='joined')


class HRTrainingAttachment(db.Model):
    __tablename__ = "hr_training_attachment"

    id = db.Column(db.Integer, primary_key=True)

    program_id = db.Column(db.Integer, db.ForeignKey('hr_training_program.id'), nullable=False, index=True)

    title = db.Column(db.String(200), nullable=True)

    original_name = db.Column(db.String(255), nullable=True)
    stored_name = db.Column(db.String(255), nullable=True)

    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)

    program = db.relationship('HRTrainingProgram', back_populates='attachments', lazy='joined')
    uploaded_by = db.relationship('User', foreign_keys=[uploaded_by_id], lazy='joined')


class HRTrainingEnrollment(db.Model):
    __tablename__ = "hr_training_enrollment"

    id = db.Column(db.Integer, primary_key=True)

    program_id = db.Column(db.Integer, db.ForeignKey('hr_training_program.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)

    status = db.Column(db.String(30), nullable=False, default="CANDIDATE", index=True)  # CANDIDATE/APPROVED/COMPLETED/WITHDRAWN
    notes = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    program = db.relationship('HRTrainingProgram', back_populates='enrollments', lazy='joined')
    user = db.relationship('User', foreign_keys=[user_id], lazy='joined')

    __table_args__ = (
        db.UniqueConstraint('program_id', 'user_id', name='uq_hr_training_enrollment_program_user'),
        db.Index('ix_hr_training_enroll_program_status', 'program_id', 'status'),
        db.Index('ix_hr_training_enroll_user_status', 'user_id', 'status'),
    )


# ======================
# Employee Evaluations
# ======================

class EmployeeEvaluationRun(db.Model):
    """System-generated evaluation snapshot for an employee.

    Period types:
      - MONTHLY: year + month
      - ANNUAL:  year only

    Stores both score_100 (0..100) and score_5 (0..5) rounded to 0.1.
    """

    __tablename__ = "employee_evaluation_run"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    period_type = db.Column(db.String(10), nullable=False, index=True)  # MONTHLY / ANNUAL
    year = db.Column(db.Integer, nullable=False, index=True)
    month = db.Column(db.Integer, nullable=True, index=True)  # only for MONTHLY

    start_date = db.Column(db.DateTime, nullable=False, index=True)
    end_date = db.Column(db.DateTime, nullable=False, index=True)

    score_100 = db.Column(db.Float, nullable=False, default=0.0)
    score_5 = db.Column(db.Float, nullable=False, default=0.0)

    # JSON string (breakdown / metrics / weights)
    breakdown_json = db.Column(db.Text, nullable=True)

    # Optional AI/summary text (can be generated later)
    summary = db.Column(db.Text, nullable=True)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    user = db.relationship("User", foreign_keys=[user_id], lazy="joined")
    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="joined")

    __table_args__ = (
        db.UniqueConstraint("user_id", "period_type", "year", "month", name="uq_eval_user_period"),
        db.Index("ix_eval_period", "period_type", "year", "month"),
    )