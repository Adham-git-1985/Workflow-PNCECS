from flask import (
    Flask, render_template, request,
    redirect, url_for, flash, send_file
)
from flask_login import (
    login_user, login_required,
    current_user, logout_user
)
from werkzeug.security import check_password_hash
from urllib.parse import urlparse
from flask_migrate import Migrate
from flask_wtf.csrf import generate_csrf
from sqlalchemy import func, event
from sqlalchemy.engine import Engine
from datetime import datetime, timedelta
import io
import os
import time
from flask import session
import logging


from utils.events import emit_event
from admin.masterdata import masterdata_bp


# ======================
# Extensions
# ======================
from extensions import db, login_manager
from sqlalchemy import text

# ======================
# Models
# ======================
from models import (
    User, WorkflowRequest,
    Approval, AuditLog, Notification,
    MessageRecipient
)

# ======================
# Blueprints
# ======================
# Blueprints
from workflow import workflow_bp
from portal import portal_bp
from admin.routes import admin_bp
from archive.routes import archive_bp
from audit.routes import audit_bp
from users.routes import users_bp
from messages import messages_bp
from delegation import delegation_bp
from store import store_bp


from filters.request_filters import apply_request_filters
from utils.permissions import get_effective_user
from filters.request_filters import get_sla_state
from services.escalation_service import run_escalation_if_needed

from filters.request_filters import get_sla_days, get_escalation_days
from flask import g

# ======================
# logging
# ======================

import logging
from logging.handlers import RotatingFileHandler
from sqlalchemy.exc import SQLAlchemyError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)

# =========================
# Logging (Workflow)
# =========================
if not os.path.exists("logs"):
    os.mkdir("logs")

file_handler = RotatingFileHandler(
    "logs/workflow.log",
    maxBytes=1_000_000,   # 1MB
    backupCount=5
)

file_handler.setLevel(logging.INFO)

formatter = logging.Formatter(
    "%(asctime)s | %(levelname)s | %(message)s"
)

file_handler.setFormatter(formatter)

logging.getLogger().setLevel(logging.INFO)
logging.getLogger().addHandler(file_handler)


ESCALATION_BADGE_CACHE = {
    "value": None,
    "last_update": None,
    "user_id": None
}

ESCALATION_BADGE_TTL = 30  # seconds

UNREAD_CACHE = {}
UNREAD_TTL = 10  # seconds

# ======================
# App Init

# ======================
# Labels (Arabic)
# ======================
ESCALATION_CATEGORY_LABELS_AR = {
    "SLA_RISK": "خطر تجاوز SLA",
    "URGENT": "عاجل",
    "MISSING_INFO": "نقص معلومات",
    "BLOCKED": "معيق/متوقف",
    "CONFLICT": "تعارض/خلاف",
    "NEED_GUIDANCE": "بحاجة لتوجيه",
    "OTHER": "أخرى",
}

def esc_category_ar(code):
    if code is None:
        return ""
    try:
        key = str(code).strip().upper()
    except Exception:
        return code
    return ESCALATION_CATEGORY_LABELS_AR.get(key, code)

# ======================

app = Flask(__name__)
app.jinja_env.globals["csrf_token"] = generate_csrf
app.jinja_env.globals["get_sla_state"] = get_sla_state
app.jinja_env.filters["esc_category_ar"] = esc_category_ar

# Cache func
def get_unread_count(user_id, source="workflow"):
    """Count unread notifications for a user within a given source scope.

    source: 'workflow' or 'portal'
    """
    now = datetime.utcnow()
    cache_key = (int(user_id), (source or 'workflow').lower())
    cache = UNREAD_CACHE.get(cache_key)

    if cache and (now - cache['ts']).seconds < UNREAD_TTL:
        return cache['value']

    src = (source or 'workflow').lower()
    if src == 'portal':
        src_filter = (Notification.source == 'portal')
    else:
        # Treat NULL as legacy workflow
        src_filter = (Notification.source.is_(None) | (Notification.source == 'workflow'))

    count = (
        db.session.query(func.count(Notification.id))
        .filter(
            Notification.user_id == user_id,
            Notification.is_mirror.is_(False),
            Notification.is_read.is_(False),
            src_filter
        )
        .scalar()
    )

    UNREAD_CACHE[cache_key] = {'value': int(count or 0), 'ts': now}
    return int(count or 0)

app.jinja_env.globals["get_unread_count"] = get_unread_count


def get_unread_messages_count(user_id):
    """Count unread internal messages for current user."""
    try:
        return (
            db.session.query(func.count(MessageRecipient.id))
            .filter(
                MessageRecipient.recipient_user_id == user_id,
                MessageRecipient.is_deleted.is_(False),
                MessageRecipient.is_read.is_(False)
            )
            .scalar()
        )
    except Exception:
        return 0


app.jinja_env.globals["get_unread_messages_count"] = get_unread_messages_count


# تأكيد وجود instance
os.makedirs(app.instance_path, exist_ok=True)

# المسار المطلق لقاعدة البيانات
db_path = os.path.join(app.instance_path, "workflow.db")

app.config["SECRET_KEY"] = "super-secret-key"
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# تحسين الأداء وتقليل البطء المتقطع مع SQLite (وخاصة مع SSE)
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_size": 10,
    "max_overflow": 20,
    "pool_timeout": 30,
    "connect_args": {
        "timeout": 30,
        "check_same_thread": False,
    },
}


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):
    """Improve SQLite concurrency/perf to reduce intermittent slowness."""
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA temp_store=MEMORY;")
        cursor.execute("PRAGMA busy_timeout=5000;")
        cursor.close()
    except Exception:
        pass

app.config.from_object("config.DevConfig")




# ======================
# Extensions Init
# ======================
db.init_app(app)


def _ensure_runtime_schema():
    """Best-effort runtime schema sync for SQLite.

    - Creates new tables (e.g., Section) via create_all.
    - Adds new columns that are required for new features without forcing the user to reset the DB.

    Safe to run on every startup.
    """
    try:
        with app.app_context():
            # create new tables if missing
            try:
                db.create_all()
            except Exception:
                pass

            # Only handle ALTER TABLE for SQLite
            try:
                if getattr(db.engine.dialect, "name", "") != "sqlite":
                    return
            except Exception:
                return

            def _col_exists(table: str, col: str) -> bool:
                try:
                    rows = db.session.execute(text(f"PRAGMA table_info({table})")).all()
                    return any(r[1] == col for r in rows)
                except Exception:
                    return False

            def _add_column_retry(table: str, col: str, ctype: str, retries: int = 5) -> bool:
                """Best-effort ALTER TABLE ADD COLUMN with simple retry for Windows/SQLite locks."""
                for i in range(retries):
                    try:
                        db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ctype}"))
                        db.session.commit()
                        return True
                    except Exception as e:
                        try:
                            db.session.rollback()
                        except Exception:
                            pass

                        msg = str(e).lower()
                        if "locked" in msg or "busy" in msg:
                            time.sleep(0.15 * (i + 1))
                            continue
                        return False
                return False

            # users.directorate_id
            if not _col_exists("users", "directorate_id"):
                try:
                    db.session.execute(text("ALTER TABLE users ADD COLUMN directorate_id INTEGER"))
                    db.session.commit()
                except Exception:
                    db.session.rollback()



            # users.unit_id / users.section_id / users.division_id (org structure)
            for _col in ("unit_id", "section_id", "division_id", "org_node_id"):
                if not _col_exists("users", _col):
                    try:
                        _add_column_retry("users", _col, "INTEGER")
                    except Exception:
                        try:
                            db.session.rollback()
                        except Exception:
                            pass

            
            # users last successful login tracking (Portal HR reports)
            for _col, _ctype in [
                ("last_login_success_at", "TEXT"),
                ("last_login_success_ip", "TEXT"),
                ("last_login_success_ua", "TEXT"),
            ]:
                if not _col_exists("users", _col):
                    _add_column_retry("users", _col, _ctype)

            # request_escalation.step_order + request_escalation.targets
            if not _col_exists("request_escalation", "step_order"):
                try:
                    db.session.execute(text("ALTER TABLE request_escalation ADD COLUMN step_order INTEGER"))
                    db.session.commit()
                except Exception:
                    db.session.rollback()

            if not _col_exists("request_escalation", "targets"):
                try:
                    db.session.execute(text("ALTER TABLE request_escalation ADD COLUMN targets TEXT"))
                    db.session.commit()
                except Exception:
                    db.session.rollback()



            # workflow steps: committee columns (best-effort for existing DBs)
            for table, col, ctype in [
                # Committee columns
                ("workflow_template_steps", "approver_committee_id", "INTEGER"),
                ("workflow_template_steps", "committee_delivery_mode", "TEXT"),
                ("workflow_template_parallel_assignees", "approver_committee_id", "INTEGER"),
                ("workflow_template_parallel_assignees", "committee_delivery_mode", "TEXT"),
                ("workflow_instance_steps", "approver_committee_id", "INTEGER"),
                ("workflow_instance_steps", "committee_delivery_mode", "TEXT"),

                # Org-structure routing targets (Units / Sections / Divisions)
                ("workflow_template_steps", "approver_unit_id", "INTEGER"),
                ("workflow_template_steps", "approver_section_id", "INTEGER"),
                ("workflow_template_steps", "approver_division_id", "INTEGER"),

                ("workflow_template_parallel_assignees", "approver_unit_id", "INTEGER"),
                ("workflow_template_parallel_assignees", "approver_section_id", "INTEGER"),
                ("workflow_template_parallel_assignees", "approver_division_id", "INTEGER"),

                ("workflow_instance_steps", "approver_unit_id", "INTEGER"),
                ("workflow_instance_steps", "approver_section_id", "INTEGER"),
                ("workflow_instance_steps", "approver_division_id", "INTEGER"),

                # Dynamic OrgNode target
                ("workflow_template_steps", "approver_org_node_id", "INTEGER"),
                ("workflow_template_parallel_assignees", "approver_org_node_id", "INTEGER"),
                ("workflow_instance_steps", "approver_org_node_id", "INTEGER"),
            ]:
                if not _col_exists(table, col):
                    try:
                        db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ctype}"))
                        db.session.commit()
                    except Exception:
                        db.session.rollback()


            # workflow_routing_rules: dynamic OrgNode scope
            for table, col, ctype in [
                ("workflow_routing_rules", "org_node_id", "INTEGER"),
                ("workflow_routing_rules", "match_subtree", "INTEGER"),  # stored as 0/1 in SQLite
            ]:
                if not _col_exists(table, col):
                    try:
                        db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ctype}"))
                        db.session.commit()
                    except Exception:
                        db.session.rollback()
            # employee_attachment: payslip period (month/year)
            for col, ctype in [
                ("payslip_year", "INTEGER"),
                ("payslip_month", "INTEGER"),
            ]:
                if not _col_exists("employee_attachment", col):
                    _add_column_retry("employee_attachment", col, ctype)

            # archived_file: final deletion columns (Super Trash)
            for col, ctype in [
                ("is_final_deleted", "INTEGER DEFAULT 0"),
                ("final_deleted_at", "TEXT"),
                ("final_deleted_by", "INTEGER"),
            ]:
                if not _col_exists("archived_file", col):
                    _add_column_retry("archived_file", col, ctype)


            # portal_access_request: assignment/routing columns
            for col, ctype in [
                ("assigned_to_user_id", "INTEGER"),
                ("assigned_role", "TEXT"),
            ]:
                if not _col_exists("portal_access_request", col):
                    _add_column_retry("portal_access_request", col, ctype)

            # notification.source (separate Portal vs Workflow notifications)
            if not _col_exists("notification", "source"):
                _add_column_retry("notification", "source", "TEXT")

            # Backfill notification.source for existing rows (best-effort)
            if _col_exists("notification", "source"):
                try:
                    db.session.execute(text(
                        "UPDATE notification SET source='portal' "
                        "WHERE source IS NULL AND ("
                        "type='PORTAL' OR message LIKE '%بوابة%' OR message LIKE '%HR Self-Service%' OR "
                        "message LIKE '%Self-Service%' OR message LIKE '%الطلبات الداخلية%'"
                        ")"
                    ))
                    db.session.execute(text("UPDATE notification SET source='workflow' WHERE source IS NULL"))
                    db.session.commit()
                except Exception:
                    try:
                        db.session.rollback()
                    except Exception:
                        pass

            
            # HR leave types: mark external leave types
            if not _col_exists("hr_leave_type", "is_external"):
                _add_column_retry("hr_leave_type", "is_external", "INTEGER DEFAULT 0")
                try:
                    db.session.execute(text("UPDATE hr_leave_type SET is_external=0 WHERE is_external IS NULL"))
                    db.session.commit()
                except Exception:
                    try:
                        db.session.rollback()
                    except Exception:
                        pass

            # HR leave types: default annual balance
            if not _col_exists("hr_leave_type", "default_balance_days"):
                _add_column_retry("hr_leave_type", "default_balance_days", "INTEGER")

            # HR leave types: exceptional max days (e.g., sick leave extension)
            if not _col_exists("hr_leave_type", "exception_max_days"):
                _add_column_retry("hr_leave_type", "exception_max_days", "INTEGER")
            if not _col_exists("hr_leave_type", "exception_requires_hr"):
                _add_column_retry("hr_leave_type", "exception_requires_hr", "INTEGER DEFAULT 1")
            if not _col_exists("hr_leave_type", "exception_requires_note"):
                _add_column_retry("hr_leave_type", "exception_requires_note", "INTEGER DEFAULT 0")

            # HR leave requests: optional external leave fields
            for col, ctype in [
                ("travel_country", "TEXT"),
                ("travel_city", "TEXT"),
                ("travel_address", "TEXT"),
                ("travel_contact_phone", "TEXT"),
                ("travel_purpose", "TEXT"),
                ("border_crossing", "TEXT"),
            ]:
                if not _col_exists("hr_leave_request", col):
                    _add_column_retry("hr_leave_request", col, ctype)

            # HR leave requests: reminders for pending approvals
            if not _col_exists("hr_leave_request", "reminder_sent_at"):
                _add_column_retry("hr_leave_request", "reminder_sent_at", "TEXT")
            if not _col_exists("hr_leave_request", "reminder_count"):
                _add_column_retry("hr_leave_request", "reminder_count", "INTEGER DEFAULT 0")

            # HR leave requests: cancellation tracking
            for col, ctype in [
                ("cancelled_at", "TEXT"),
                ("cancelled_by_id", "INTEGER"),
                ("cancelled_from_status", "TEXT"),
                ("cancel_note", "TEXT"),
                ("cancel_effective_date", "TEXT"),
            ]:
                if not _col_exists("hr_leave_request", col):
                    _add_column_retry("hr_leave_request", col, ctype)

            # HR permission requests: cancellation tracking
            for col, ctype in [
                ("cancelled_at", "TEXT"),
                ("cancelled_by_id", "INTEGER"),
                ("cancelled_from_status", "TEXT"),
            ]:
                if not _col_exists("hr_permission_request", col):
                    _add_column_retry("hr_permission_request", col, ctype)



            # Transport (Fleet) columns
            for col, ctype in [
                ("manufacture_day", "TEXT"),
                ("fuel_card_no", "TEXT"),
                ("fuel_type_lookup_id", "INTEGER"),
                ("service_start_day", "TEXT"),
                ("license_end_day", "TEXT"),
                ("insurance_end_day", "TEXT"),
                ("work_location_lookup_id", "INTEGER"),
                ("consumption_rate", "REAL"),
                ("max_fuel_limit", "REAL"),
            ]:
                if not _col_exists("transport_vehicle", col):
                    _add_column_retry("transport_vehicle", col, ctype)

            # Transport soft delete + trip extra fields
            for col, ctype in [
                ("is_deleted", "INTEGER DEFAULT 0"),
                ("deleted_at", "TEXT"),
                ("deleted_by_id", "INTEGER"),
            ]:
                if not _col_exists("transport_permit", col):
                    _add_column_retry("transport_permit", col, ctype)
                if not _col_exists("transport_trip", col):
                    _add_column_retry("transport_trip", col, ctype)

            for col, ctype in [
                ("order_no", "TEXT"),
                ("place_kind", "TEXT"),
            ]:
                if not _col_exists("transport_trip", col):
                    _add_column_retry("transport_trip", col, ctype)

            # Units: ensure units.organization_id exists (Units are under Organization)
            if not _col_exists("units", "organization_id"):
                _add_column_retry("units", "organization_id", "INTEGER")

            # Backfill units.organization_id from legacy units.directorate_id (best-effort)
            if _col_exists("units", "organization_id") and _col_exists("units", "directorate_id"):
                try:
                    db.session.execute(text(
                        "UPDATE units SET organization_id = ("
                        "SELECT organization_id FROM directorates WHERE directorates.id = units.directorate_id"
                        ") WHERE organization_id IS NULL AND directorate_id IS NOT NULL"
                    ))
                    db.session.commit()
                except Exception:
                    try:
                        db.session.rollback()
                    except Exception:
                        pass


            # HR Training: publish_conditions_only (published by conditions only)
            if not _col_exists("hr_training_program", "publish_conditions_only"):
                _add_column_retry("hr_training_program", "publish_conditions_only", "INTEGER DEFAULT 0")


            # backfill directorate_id from department_id for existing users
            if _col_exists("users", "directorate_id"):
                try:
                    db.session.execute(text(
                        "UPDATE users SET directorate_id = (SELECT directorate_id FROM departments WHERE departments.id = users.department_id) "
                        "WHERE directorate_id IS NULL AND department_id IS NOT NULL"
                    ))
                    db.session.commit()
                except Exception:
                    db.session.rollback()

            # Seed dynamic org structure (types + one-time legacy sync)
            try:
                from utils.org_dynamic import ensure_dynamic_org_seed
                ensure_dynamic_org_seed()
            except Exception:
                try:
                    db.session.rollback()
                except Exception:
                    pass

            # -------------------------
            # Seed "basic" permissions (RolePermission)
            # -------------------------
            # We keep SIGN_ARCHIVE as a permission hook, but make it available by default
            # for all active roles so any authenticated user can use the signing feature.
            try:
                from sqlalchemy import func
                from models import Role, RolePermission

                perm = "SIGN_ARCHIVE"
                roles = Role.query.filter_by(is_active=True).all()
                changed = 0
                for r in roles or []:
                    code = (getattr(r, "code", "") or "").strip()
                    if not code:
                        continue
                    exists = (
                        RolePermission.query
                        .filter(func.lower(RolePermission.role) == code.lower())
                        .filter(RolePermission.permission == perm)
                        .first()
                    )
                    if not exists:
                        db.session.add(RolePermission(role=code, permission=perm))
                        changed += 1
                if changed:
                    db.session.commit()
            except Exception:
                try:
                    db.session.rollback()
                except Exception:
                    pass
    except Exception:
        # do not block app startup
        try:
            db.session.rollback()
        except Exception:
            pass


# NOTE (Windows/SQLite): init_db.py may need to delete/replace the DB file.
# Importing this module previously triggered a connection to SQLite via
# _ensure_runtime_schema(), which locks the file on Windows and prevents removal.
# We allow scripts (like init_db.py) to skip this best-effort runtime schema sync
# by setting SKIP_RUNTIME_SCHEMA=1.
if not os.getenv("SKIP_RUNTIME_SCHEMA"):
    _ensure_runtime_schema()
login_manager.init_app(app)
login_manager.login_view = "login"
migrate = Migrate(app, db)

# ======================
# Register Blueprints
# ======================
app.register_blueprint(admin_bp)
app.register_blueprint(audit_bp)
app.register_blueprint(users_bp)
app.register_blueprint(archive_bp)
app.register_blueprint(workflow_bp)
app.register_blueprint(portal_bp)
app.register_blueprint(store_bp)


# ======================
# Error Handlers
# ======================

@app.errorhandler(401)
def _handle_401(err):
    """Redirect unauthenticated users to login (with next=...)."""
    try:
        # For API/AJAX calls, return JSON
        if request.accept_mimetypes.best == "application/json" or request.path.startswith("/api"):
            from flask import jsonify
            return jsonify({"error": "unauthorized", "message": "يجب تسجيل الدخول أولاً"}), 401
    except Exception:
        pass
    return redirect(url_for("login", next=request.full_path))


@app.errorhandler(403)
def _handle_403(err):
    """Show a friendly Arabic permission message instead of the default Werkzeug page."""
    try:
        if request.accept_mimetypes.best == "application/json" or request.path.startswith("/api"):
            from flask import jsonify
            return jsonify({"error": "forbidden", "message": "لا يوجد لديك صلاحية"}), 403
    except Exception:
        pass
    return render_template("errors/403.html"), 403


# ----------------------------
# Background jobs (in-process)
# ----------------------------
# Timeclock file auto-sync (optional): polls the configured server file and syncs
# attendance events when it changes. Can be controlled from Portal → Admin → Integrations.
#
# Flask 3 removed before_first_request; and some environments may not have before_serving.
# We start jobs on the first real request, once per process.
try:
    from portal.timeclock_auto import start_timeclock_auto_sync
    from portal.hr_alerts_job import start_hr_alerts_job

    _jobs_started = False

    @app.before_request
    def _start_jobs_once():
        global _jobs_started
        if _jobs_started:
            return
        # Only start on real endpoints (avoid static assets if desired)
        try:
            from flask import request
            if request.endpoint is None:
                return
        except Exception:
            pass

        _jobs_started = True
        try:
            start_timeclock_auto_sync(app)
            start_hr_alerts_job(app)
        except Exception:
            # Keep serving even if job fails
            app.logger.exception("Failed to start timeclock auto-sync")
except Exception as _e:
    # Don't fail the whole app if background job wiring fails
    app.logger.exception("Failed to wire timeclock auto-sync: %s", _e)
app.register_blueprint(masterdata_bp)
app.register_blueprint(messages_bp)
app.register_blueprint(delegation_bp)


# ======================
# Login Manager
# ======================
@login_manager.user_loader
def load_user(user_id):
    """
    Final, safe, and optimized user_loader
    - Uses db.session.get (SQLAlchemy 2.0 safe)
    - Caches user per request (via flask.g)
    - Logs only meaningful events
    """

    # Validate user_id early
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        logger.warning(f"user_loader invalid user_id: {user_id}")
        return None

    # Return cached user if already loaded in this request
    if hasattr(g, "_current_user"):
        logger.debug("user_loader: using cached user")
        return g._current_user

    # Load user safely
    try:
        user = db.session.get(User, uid)

        if user is None:
            logger.warning(f"user_loader: user not found (id={uid})")
        else:
            logger.debug(f"user_loader: loaded user id={user.id}, email={user.email}")

    except SQLAlchemyError as e:
        logger.exception(f"user_loader DB error for user_id={uid}")
        return None

    # Cache result for this request
    g._current_user = user
    return user


# ======================
# Workflow Constants
# ======================
STATUS_ROLE_MAP = {
    "SUBMITTED": "dept_head",
    "DEPT_REVIEW": "finance",
    "FIN_REVIEW": "secretary_general"
}

NEXT_STATUS_MAP = {
    "SUBMITTED": "DEPT_REVIEW",
    "DEPT_REVIEW": "FIN_REVIEW",
    "FIN_REVIEW": "APPROVED"
}

REJECT_STATUS = "REJECTED"
FINAL_STATUSES = ["APPROVED", "REJECTED"]


@app.route("/")
@login_required
def index():
    logger.info(
        f"Accessing index | is_authenticated={current_user.is_authenticated} | user={current_user.get_id()}"
    )

    # ✅ Default landing page for all users after login: "مهماتي" (Inbox)
    # Admins can still navigate to admin pages from the sidebar/navbar.
    return redirect(url_for("workflow.inbox"))

@app.teardown_appcontext
def shutdown_session(exception=None):
    db.session.remove()


@app.before_request
def log_session():
    logger.debug(f"Session content: {dict(session)}")
    try:
        if getattr(current_user, 'is_authenticated', False):
            get_effective_user()  # loads g.delegation / g.effective_user
    except Exception:
        pass

@login_manager.unauthorized_handler
def unauthorized():
    logger.warning(
        f"Unauthorized access | path={request.path} | user={current_user.get_id()}"
    )
    return redirect(url_for("login"))


# ======================
# Auth Routes
# ======================
@app.route("/login", methods=["GET", "POST"])
def login():

    logger.info("Login page accessed")

    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")

        logger.info(f"Login attempt for email={email}")

        user = User.query.filter_by(email=email).first()

        if not user:
            logger.warning("Login failed: user not found")
            flash("Invalid credentials")
            return redirect(url_for("login"))

        if not user.check_password(password):
            logger.warning("Login failed: wrong password")
            flash("Invalid credentials")
            return redirect(url_for("login"))

        # Track last successful login (for HR report)
        try:
            user.last_login_success_at = datetime.utcnow()
            ip = None
            try:
                if request.headers.get('X-Forwarded-For'):
                    ip = request.headers.get('X-Forwarded-For').split(',')[0].strip()
            except Exception:
                ip = None
            if not ip:
                try:
                    ip = request.remote_addr
                except Exception:
                    ip = None
            user.last_login_success_ip = (ip or '')[:64] or None

            ua = None
            try:
                ua = request.headers.get('User-Agent')
            except Exception:
                ua = None
            user.last_login_success_ua = (ua or '')[:255] or None

            db.session.commit()
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass

        login_user(user)
        logger.info(
            f"Login success | user_id={user.id} | authenticated={current_user.is_authenticated}"
        )

        return redirect(url_for("index"))

    return render_template("login.html", disable_sse=True, hide_sidebar=True)

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

# ======================
# Create Request
# ======================
@app.route("/request/new", methods=["GET", "POST"])
@login_required
def create_request():
    if request.method == "POST":
        title = request.form.get("title")
        description = request.form.get("description")
        action = request.form.get("action")

        status = "DRAFT" if action == "save" else "SUBMITTED"
        current_role = STATUS_ROLE_MAP.get(status)

        new_request = WorkflowRequest(
            title=title,
            description=description,
            status=status,
            requester_id=current_user.id,
            current_role=current_role
        )

        db.session.add(new_request)
        db.session.flush()

        #  Audit (موجود عندك)
        log_action(
            request_obj=new_request,
            user=current_user,
            action="CREATE_REQUEST",
            old_status=None,
            new_status=status,
            note="تم إنشاء الطلب"
        )

        #  Notification + Audit مركزي
        emit_event(
            actor_id=current_user.id,
            action="REQUEST_CREATED",
            message=f"تم إنشاء طلب جديد رقم #{new_request.title}",
            target_type="WorkflowRequest",
            target_id=new_request.id,
            notify_role="ADMIN"
        )

        db.session.commit()
        flash("تم حفظ الطلب بنجاح", "success")
        return redirect(url_for("create_request"))
    return render_template("create_request.html")

# ======================
# My Requests
# ======================
@app.route("/my-requests")
@login_required
def my_requests():
    status = request.args.get("status")

    base_query = WorkflowRequest.query.filter_by(
        requester_id=current_user.id
    )

    counters = {
        "total": base_query.count(),
        "approved": base_query.filter_by(status="APPROVED").count(),
        "rejected": base_query.filter_by(status="REJECTED").count(),
        "draft": base_query.filter_by(status="DRAFT").count(),
        "in_progress": base_query.filter(
            WorkflowRequest.status.notin_(
                FINAL_STATUSES + ["DRAFT"]
            )
        ).count()
    }

    if status:
        if status == "in_progress":
            base_query = base_query.filter(
                WorkflowRequest.status.notin_(
                    FINAL_STATUSES + ["DRAFT"]
                )
            )
        else:
            base_query = base_query.filter(
                WorkflowRequest.status == status.upper()
            )

    requests = base_query.order_by(
        WorkflowRequest.id.desc()
    ).all()


    return render_template(
        "my_requests.html",
        requests=requests,
        counters=counters,
        last_update=datetime.utcnow()
    )

# ======================
# Inbox
# ======================
@app.route("/inbox")
@login_required
def inbox():
    run_escalation_if_needed()
    effective_user = get_effective_user()


    # 1️⃣ Base query (delegation-aware)
    base_query = WorkflowRequest.query.filter(
        WorkflowRequest.current_role == effective_user.role
    )

    # 2️⃣ Apply advanced filters
    filtered_query = apply_request_filters(
        base_query,
        request.args
    )

    # 3️⃣ Counters (من نفس filtered_query)
    counters = {
        "total": filtered_query.count(),
        "approved": filtered_query.filter(
            WorkflowRequest.status == "APPROVED"
        ).count(),
        "rejected": filtered_query.filter(
            WorkflowRequest.status == "REJECTED"
        ).count(),
        "in_progress": filtered_query.filter(
            WorkflowRequest.status.notin_(["APPROVED", "REJECTED"])
        ).count(),
    }

    sla_days = get_sla_days()
    esc_days = get_escalation_days()

    now = datetime.utcnow()
    sla_deadline = now - timedelta(days=sla_days)
    esc_deadline = now - timedelta(days=sla_days + esc_days)

    sla_counters = {
        "on_track": filtered_query.filter(
            WorkflowRequest.status.notin_(["APPROVED", "REJECTED"]),
            WorkflowRequest.created_at >= sla_deadline
        ).count(),

        "breached": filtered_query.filter(
            WorkflowRequest.status.notin_(["APPROVED", "REJECTED"]),
            WorkflowRequest.created_at < sla_deadline,
            WorkflowRequest.created_at >= esc_deadline
        ).count(),

        "escalated": filtered_query.filter(
            WorkflowRequest.status.notin_(["APPROVED", "REJECTED"]),
            WorkflowRequest.created_at < esc_deadline
        ).count(),
    }

    now = datetime.utcnow()
    esc_deadline = now - timedelta(
        days=get_sla_days() + get_escalation_days()
    )


    if (
            ESCALATION_BADGE_CACHE["value"] is not None
            and ESCALATION_BADGE_CACHE["last_update"]
            and ESCALATION_BADGE_CACHE["user_id"] == effective_user.id
            and (now - ESCALATION_BADGE_CACHE["last_update"]).seconds < ESCALATION_BADGE_TTL
    ):
        escalation_alerts_count = ESCALATION_BADGE_CACHE["value"]
    else:
        escalation_alerts_count = WorkflowRequest.query.filter(
            WorkflowRequest.current_role == effective_user.id,
            WorkflowRequest.status.notin_(["APPROVED", "REJECTED"]),
            WorkflowRequest.created_at < esc_deadline
        ).count()

        ESCALATION_BADGE_CACHE["value"] = escalation_alerts_count
        ESCALATION_BADGE_CACHE["last_update"] = now
        ESCALATION_BADGE_CACHE["user_id"] = effective_user.id

    # 4️⃣ Final list
    requests = filtered_query.order_by(
        WorkflowRequest.created_at.desc()
    ).all()

    return render_template(
        "inbox.html",
        requests=requests,
        counters=counters,
        sla_counters=sla_counters,
        escalation_alerts_count=escalation_alerts_count,
        is_admin=False,
        get_sla_state=get_sla_state
    )


# ======================
# Review / Actions
# ======================
@app.route("/request/<int:request_id>")
@login_required
def review_request(request_id):
    req = WorkflowRequest.query.get_or_404(request_id)

    # إذا كان الطلب يتبع Workflow Engine الجديد، حوّله لصفحة العرض الجديدة
    # (لتفادي تضارب المسارات القديمة current_role)
    if getattr(req, "workflow_instance", None):
        return redirect(url_for("workflow.view_request", request_id=req.id))

    if req.current_role != current_user.role:
        flash("غير مصرح لك بمراجعة هذا الطلب", "danger")
        return redirect(url_for("inbox"))

    return render_template("review_request.html", req=req)


@app.route("/request/<int:request_id>/action", methods=["POST"])
@login_required
def request_action(request_id):
    req = WorkflowRequest.query.get_or_404(request_id)

    # هذا المسار قديم ولا يجب أن ينفّذ إجراءات على طلبات Workflow Engine
    if getattr(req, "workflow_instance", None):
        flash("هذا الطلب يعمل عبر محرك المسارات الجديد. استخدم صفحة الطلب ضمن /workflow.", "info")
        return redirect(url_for("workflow.view_request", request_id=req.id))

    if req.current_role != current_user.role:
        flash("غير مصرح لك بتنفيذ هذا الإجراء", "danger")
        return redirect(url_for("inbox"))

    action = request.form.get("action")
    note = request.form.get("note")
    old_status = req.status
    old_role = req.current_role

    if action == "approve":
        req.status = NEXT_STATUS_MAP.get(req.status, req.status)
        req.current_role = STATUS_ROLE_MAP.get(req.status)
        action_name = "APPROVE"
        notif_type = "INFO"
    else:
        req.status = REJECT_STATUS
        req.current_role = None
        action_name = "REJECT"
        notif_type = "CRITICAL"

    log_action(
        request_obj=req,
        user=current_user,
        action=action_name,
        old_status=old_status,
        new_status=req.status,
        note=note
    )

    # 🔔 Notification + Audit
    emit_event(
        actor_id=current_user.id,
        action=f"REQUEST_{action_name}",
        message=(
            f"تم {'الموافقة على' if action == 'approve' else 'رفض'} "
            f"الطلب رقم #{req.id}"
        ),
        target_type="WorkflowRequest",
        target_id=req.id,
        notify_user_id=req.requester_id,  # صاحب الطلب
        notif_type=notif_type
    )

    db.session.add(Approval(
        request_id=req.id,
        user_id=current_user.id,
        action=action,
        note=note
    ))

    db.session.commit()

    flash("تم تسجيل الإجراء بنجاح", "success")
    return redirect(url_for("inbox"))

# ======================
# Audit / PDF
# ======================
def log_action(request_obj, user, action, old_status, new_status, note=None):
    db.session.add(AuditLog(
        request_id=request_obj.id,
        user_id=user.id if user else None,
        action=action,
        old_status=old_status,
        new_status=new_status,
        note=note
    ))


@app.route("/request/<int:request_id>/audit")
@login_required
def request_audit(request_id):
    logs = AuditLog.query.filter_by(
        request_id=request_id
    ).order_by(AuditLog.created_at.asc()).all()

    req = WorkflowRequest.query.get_or_404(request_id)
    return render_template("audit_log.html", logs=logs, req=req)



if __name__ == "__main__":
    app.run(debug=True, use_reloader=False)

