"""
init_db.py
----------
Initialize the database and seed initial users.
DEVELOPMENT USE ONLY
"""

import os
import re
from sqlalchemy import text
from werkzeug.security import generate_password_hash

try:
    from openpyxl import load_workbook
except Exception:
    load_workbook = None

from app import app
from extensions import db

# ⚠️ مهم: استيراد كل الـ Models لتسجيل الجداول
from models import (
    User, ArchivedFile, FilePermission, AuditLog, Notification,
    WorkflowRequest, RequestAttachment,
    WorkflowTemplate, WorkflowTemplateStep, WorkflowInstance, WorkflowInstanceStep,
    Organization, Directorate, Department
)


DB_PATH = os.path.join(app.instance_path, "workflow.db")


def init_database():
    with app.app_context():

        import models
        # =========================
        # 1️⃣ حذف قاعدة البيانات القديمة
        # =========================
        if os.path.exists(DB_PATH):
            print("Removing existing database...")
            os.remove(DB_PATH)

        # =========================
        # 2️⃣ إنشاء الجداول
        # =========================
        print("Creating database tables...")
        db.create_all()

        # =========================
        # Helper: list existing tables (SQLite)
        # =========================
        tables = {
            row[0]
            for row in db.session.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).all()
        }

        def has_table(name: str) -> bool:
            return name in tables

        # =========================
        # 3️⃣ إنشاء INDEXES
        # =========================
        print("Creating indexes...")

        # ===== Notification =====
        if has_table("notification"):
            db.session.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_notification_user_read
                ON notification (user_id, is_read);
            """))
            db.session.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_notification_created
                ON notification (created_at DESC);
            """))

        # ===== Audit Log =====
        if has_table("audit_log"):
            db.session.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_audit_user_created
                ON audit_log (user_id, created_at DESC);
            """))
            db.session.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_audit_target_created
                ON audit_log (target_type, target_id, created_at DESC);
            """))

        # ===== Archive =====
        if has_table("archived_file"):
            db.session.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_archived_file_owner
                ON archived_file (owner_id);
            """))
            db.session.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_archived_file_created
                ON archived_file (upload_date DESC);
            """))
            db.session.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_archived_file_deleted
                ON archived_file (is_deleted, upload_date DESC);
            """))

        # ===== File Permission =====
        if has_table("file_permission"):
            db.session.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_file_permission_user
                ON file_permission (user_id);
            """))
            db.session.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_file_permission_file
                ON file_permission (file_id);
            """))
            db.session.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_file_permission_expiry
                ON file_permission (expires_at);
            """))

        # ===== WorkflowRequest (default table name if no __tablename__) =====
        # if your model has __tablename__ then adjust here.
        if has_table("workflow_request"):
            db.session.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_workflow_request_requester
                ON workflow_request (requester_id);
            """))
            db.session.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_workflow_request_status
                ON workflow_request (status);
            """))
            db.session.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_workflow_request_created
                ON workflow_request (created_at DESC);
            """))
            db.session.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_workflow_request_role
                ON workflow_request (current_role);
            """))

        # ===== Attachments table (we set __tablename__ = workflow_request_attachments) =====
        if has_table("workflow_request_attachments"):
            db.session.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_wra_request
                ON workflow_request_attachments (request_id);
            """))
            db.session.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_wra_archived_file
                ON workflow_request_attachments (archived_file_id);
            """))

        # ===== Optional: workflow instances/steps if you created them =====
        if has_table("workflow_instances"):
            db.session.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_workflow_instances_request
                ON workflow_instances (request_id);
            """))
            db.session.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_workflow_instances_current
                ON workflow_instances (current_step_order, is_completed);
            """))

        if has_table("workflow_instance_steps"):
            db.session.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_wis_instance_order
                ON workflow_instance_steps (instance_id, step_order);
            """))
            db.session.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_wis_status_due
                ON workflow_instance_steps (status, due_at);
            """))

        if has_table("workflow_templates"):
            db.session.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_workflow_templates_active
                ON workflow_templates (is_active);
            """))
            db.session.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_workflow_templates_created
                ON workflow_templates (created_at DESC);
            """))

        if has_table("workflow_template_steps"):
            db.session.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_wts_template_order
                ON workflow_template_steps (template_id, step_order);
            """))

        
        # =========================
        # =========================
        # 4) Seed Organization / Directorates / Departments + Users from seed.xlsx
        # =========================
        # Notes:
        # - The script will create the DB at: <instance_path>/workflow.db
        # - Place seed.xlsx next to init_db.py (recommended) OR set env var SEED_XLSX to a full path.
        # - If openpyxl is missing, seeding will STOP by default (to avoid silent empty seed).
        print(f"Database path: {DB_PATH}")
        print(f"Instance path: {app.instance_path}")

        SEED_OPTIONAL = os.environ.get("SEED_OPTIONAL", "").strip() in ("1", "true", "TRUE", "yes", "YES")
        SEED_XLSX_CANDIDATES = [
            os.environ.get("SEED_XLSX"),
            os.path.join(os.path.dirname(__file__), "seed.xlsx"),
            os.path.join(os.getcwd(), "seed.xlsx"),
        ]
        SEED_XLSX_CANDIDATES = [p for p in SEED_XLSX_CANDIDATES if p]

        def _norm(v):
            if v is None:
                return ""
            s = str(v).strip()
            s = re.sub(r"\s+", " ", s)
            return s

        def _norm_header(h: str) -> str:
            s = _norm(h)
            # Arabic normalization for headers
            s = s.replace("إ", "ا").replace("أ", "ا").replace("آ", "ا")
            s = s.replace("ى", "ي").replace("ة", "ه")
            s = re.sub(r"[\u0640]", "", s)   # tatweel
            s = re.sub(r"[\s\-_/]+", "", s)
            return s.lower()

        def _guess_role(job_title: str) -> str:
            t = _norm(job_title)
            t2 = t.replace("مديرعام", "مدير عام")
            if not t2:
                return "USER"
            # secretary general
            if ("امين عام" in t2) or ("الأمين العام" in t2) or ("الامين العام" in t2):
                return "SECRETARY_GENERAL"
            # directorate head (general manager)
            if "مدير عام" in t2:
                return "DIRECTORATE_HEAD"
            # finance keyword (optional)
            if "مالية" in t2 or "المالي" in t2:
                return "FINANCE"
            # department head
            if "مدير" in t2:
                return "DEPT_HEAD"
            return "USER"

        def _resolve_seed_path() -> str | None:
            for p in SEED_XLSX_CANDIDATES:
                try:
                    if p and os.path.exists(p):
                        return p
                except Exception:
                    continue
            return None

        def seed_from_excel() -> int:
            seed_path = _resolve_seed_path()
            if not seed_path:
                msg = (
                    "[seed] seed.xlsx NOT FOUND.\n"
                    "  - Put seed.xlsx next to init_db.py OR\n"
                    "  - Set env var SEED_XLSX to the full path of the file.\n"
                    "  - Current candidates: " + ", ".join(SEED_XLSX_CANDIDATES)
                )
                if SEED_OPTIONAL:
                    print(msg)
                    print("[seed] SEED_OPTIONAL=1 -> skipping seeding.")
                    return 0
                raise SystemExit(msg)

            if load_workbook is None:
                msg = (
                    "[seed] openpyxl is required to read seed.xlsx.\n"
                    "Install it then re-run:  pip install openpyxl\n"
                    f"Seed file path: {seed_path}"
                )
                if SEED_OPTIONAL:
                    print(msg)
                    print("[seed] SEED_OPTIONAL=1 -> skipping seeding.")
                    return 0
                raise SystemExit(msg)

            print(f"[seed] Using seed file: {seed_path}")

            wb = load_workbook(seed_path, data_only=True)

            # Find a sheet that contains required headers
            ws = None
            needed = {
                _norm_header("الإسم"): ("الإسم", "الاسم", "الإسم الكامل"),
                _norm_header("الإدارة"): ("الإدارة", "الادارة"),
                _norm_header("الدور"): ("الدور", "المسمى الوظيفي", "الوظيفة"),
                _norm_header("الايميل"): ("الايميل", "الإيميل", "البريد الالكتروني", "البريد الإلكتروني", "email"),
            }

            def build_header_map(sheet):
                headers_norm = {}
                for col in range(1, sheet.max_column + 1):
                    h = sheet.cell(1, col).value
                    if not h:
                        continue
                    headers_norm[_norm_header(h)] = col
                return headers_norm

            for name in wb.sheetnames:
                sh = wb[name]
                hm = build_header_map(sh)
                ok = True
                for k_norm, aliases in needed.items():
                    found = False
                    for a in aliases:
                        if _norm_header(a) in hm:
                            found = True
                            break
                    if not found:
                        ok = False
                        break
                if ok:
                    ws = sh
                    break

            if ws is None:
                # fall back to active sheet
                ws = wb.active

            headers_norm = build_header_map(ws)

            def col_of(*aliases):
                for a in aliases:
                    k = _norm_header(a)
                    if k in headers_norm:
                        return headers_norm[k]
                return None

            c_name = col_of("الإسم", "الاسم", "الإسم الكامل")
            c_dir = col_of("الإدارة", "الادارة")
            c_dept = col_of("الدائرة", "الدائره", "القسم")
            c_title = col_of("الدور", "المسمى الوظيفي", "الوظيفة")
            c_email = col_of("الايميل", "الإيميل", "البريد الالكتروني", "البريد الإلكتروني", "email")

            if not all([c_name, c_dir, c_title, c_email]):
                msg = (
                    "[seed] Missing required columns in seed.xlsx.\n"
                    "Expected headers like: (الإسم/الاسم), (الإدارة/الادارة), (الدور/المسمى الوظيفي), (الايميل/البريد الإلكتروني)."
                )
                raise SystemExit(msg)

            # Create (or get) base organization
            org = Organization.query.filter_by(code="PNCECS").first()
            if not org:
                org = Organization(
                    name_ar="اللجنة الوطنية الفلسطينية للتربية والثقافة والعلوم",
                    name_en="Palestinian National Commission for Education, Culture and Science",
                    code="PNCECS",
                    is_active=True,
                )
                db.session.add(org)
                db.session.flush()

            # caches
            dir_cache = {}    # directorate name -> Directorate
            dept_cache = {}   # (directorate_id, dept_name) -> Department

            def get_or_create_directorate(name_ar: str) -> Directorate:
                name_ar = _norm(name_ar)
                if name_ar == "الأمين العام":
                    name_ar = "الأمانة العامة"
                if not name_ar:
                    name_ar = "غير محددة"

                if name_ar in dir_cache:
                    return dir_cache[name_ar]

                d = Directorate.query.filter_by(organization_id=org.id, name_ar=name_ar).first()
                if not d:
                    d = Directorate(organization_id=org.id, name_ar=name_ar, is_active=True)
                    db.session.add(d)
                    db.session.flush()
                dir_cache[name_ar] = d
                return d

            def get_or_create_department(directorate_id: int, name_ar: str) -> Department:
                name_ar = _norm(name_ar)
                if not name_ar:
                    name_ar = "غير محددة"
                key = (directorate_id, name_ar)
                if key in dept_cache:
                    return dept_cache[key]

                dep = Department.query.filter_by(directorate_id=directorate_id, name_ar=name_ar).first()
                if not dep:
                    dep = Department(directorate_id=directorate_id, name_ar=name_ar, is_active=True)
                    db.session.add(dep)
                    db.session.flush()
                dept_cache[key] = dep
                return dep

            created_users = 0

            for r in range(2, ws.max_row + 1):
                name = _norm(ws.cell(r, c_name).value)
                dir_name = _norm(ws.cell(r, c_dir).value)
                dept_name = _norm(ws.cell(r, c_dept).value) if c_dept else ""
                title = _norm(ws.cell(r, c_title).value)
                email = _norm(ws.cell(r, c_email).value).lower()

                # skip empty rows
                if not any([name, dir_name, dept_name, title, email]):
                    continue

                dir_obj = get_or_create_directorate(dir_name)

                # if department is missing, create an "office" department so department_id is always set
                if not dept_name:
                    dept_name = f"مكتب {dir_obj.name_ar}"
                dept_obj = get_or_create_department(dir_obj.id, dept_name)

                if not email:
                    safe = re.sub(r"[^a-z0-9]+", ".", name.lower(), flags=re.I).strip(".")
                    email = f"{safe or 'user'}{r}@pncecs.local"

                role_code = _guess_role(title)

                u = User.query.filter_by(email=email).first()
                if not u:
                    u = User(
                        email=email,
                        name=name or None,
                        job_title=title or None,
                        password_hash=generate_password_hash("123"),
                        role=role_code,
                        department_id=dept_obj.id,
                    )
                    db.session.add(u)
                    created_users += 1
                else:
                    u.name = name or u.name
                    u.job_title = title or u.job_title
                    u.department_id = dept_obj.id
                    u.role = u.role or role_code

            print(f"[seed] Done. Created users: {created_users}")
            return created_users

        # Run the Excel seeder first (so org structure exists)
        seed_from_excel()
        # 4️⃣ Seed Users (FIXED)
        # =========================
        def get_or_create_user(email, password, role="USER", department_id=None):
            u = User.query.filter_by(email=email).first()
            if u:
                return u, False
            u = User(
                email=email,
                password_hash=generate_password_hash(password),
                role=role,
                department_id=department_id
            )
            db.session.add(u)
            return u, True

        seeded = []

        admin_email = "admin@pncecs.org"
        admin_password = "123"
        u, created = get_or_create_user(admin_email, admin_password, role="ADMIN")
        if created:
            print("Admin user created")
        seeded.append(("ADMIN", admin_email, admin_password))

        # Default SUPER_ADMIN (inherits all Admin permissions)
        super_email = "superadmin@pncecs.org"
        super_password = "123"
        u, created = get_or_create_user(super_email, super_password, role="SUPER_ADMIN")
        if created:
            print("SUPER_ADMIN user created")
        seeded.append(("SUPER_ADMIN", super_email, super_password))

        user1_email = "adham.pncecs@gmail.com"
        user1_password = "123"
        u, created = get_or_create_user(user1_email, user1_password, role="USER")
        if created:
            print("User created:", user1_email)
        seeded.append(("USER", user1_email, user1_password))

        user2_email = "mo@gmail.com"
        user2_password = "123"
        u, created = get_or_create_user(user2_email, user2_password, role="USER")
        if created:
            print("User created:", user2_email)
        seeded.append(("USER", user2_email, user2_password))

        user3_email = "ta.pncecs@gmail.com"
        user3_password = "123"
        u, created = get_or_create_user(user3_email, user3_password, role="USER")
        if created:
            print("User created:", user3_email)
        seeded.append(("USER", user3_email, user3_password))

        # =========================
        # 5️⃣ Commit نهائي
        # =========================
        db.session.commit()

        print("===================================")
        print("Database initialized successfully")
        print("===================================")
        print("Login credentials:")
        for role, em, pw in seeded:
            print(f"{role:<12} -> {em} / {pw}")


if __name__ == "__main__":
    init_database()