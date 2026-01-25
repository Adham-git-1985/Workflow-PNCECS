"""
init_db.py
----------
Initialize the database and seed initial users.
⚠️ DEVELOPMENT USE ONLY
"""

import os
from sqlalchemy import text
from werkzeug.security import generate_password_hash

from app import app
from extensions import db

# ⚠️ مهم: استيراد كل الـ Models لتسجيل الجداول
from models import (
    User, ArchivedFile, FilePermission, AuditLog, Notification,
    WorkflowRequest, RequestAttachment,
    WorkflowTemplate, WorkflowTemplateStep, WorkflowInstance, WorkflowInstanceStep
)


DB_PATH = os.path.join(app.instance_path, "workflow.db")


def init_database():
    with app.app_context():

        import models
        # =========================
        # 1️⃣ حذف قاعدة البيانات القديمة
        # =========================
        if os.path.exists(DB_PATH):
            print("🗑 Removing existing database...")
            os.remove(DB_PATH)

        # =========================
        # 2️⃣ إنشاء الجداول
        # =========================
        print("📦 Creating database tables...")
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
        print("⚡ Creating indexes...")

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
            print("👑 Admin user created")
        seeded.append(("ADMIN", admin_email, admin_password))

        # Default SUPER_ADMIN (inherits all Admin permissions)
        super_email = "superadmin@pncecs.org"
        super_password = "123"
        u, created = get_or_create_user(super_email, super_password, role="SUPER_ADMIN")
        if created:
            print("👑 SUPER_ADMIN user created")
        seeded.append(("SUPER_ADMIN", super_email, super_password))

        user1_email = "adham.pncecs@gmail.com"
        user1_password = "123"
        u, created = get_or_create_user(user1_email, user1_password, role="USER")
        if created:
            print("👤 User created:", user1_email)
        seeded.append(("USER", user1_email, user1_password))

        user2_email = "mo@gmail.com"
        user2_password = "123"
        u, created = get_or_create_user(user2_email, user2_password, role="USER")
        if created:
            print("👤 User created:", user2_email)
        seeded.append(("USER", user2_email, user2_password))

        user3_email = "ta.pncecs@gmail.com"
        user3_password = "123"
        u, created = get_or_create_user(user3_email, user3_password, role="USER")
        if created:
            print("👤 User created:", user3_email)
        seeded.append(("USER", user3_email, user3_password))

        # =========================
        # 5️⃣ Commit نهائي
        # =========================
        db.session.commit()

        print("===================================")
        print("✅ Database initialized successfully")
        print("===================================")
        print("Login credentials:")
        for role, em, pw in seeded:
            print(f"{role:<6} → {em} / {pw}")


if __name__ == "__main__":
    init_database()
