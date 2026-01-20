"""
init_db.py
----------
Initialize the database and seed initial users.
⚠️ DEVELOPMENT USE ONLY
"""

import os
from app import app
from extensions import db
from models import User
from werkzeug.security import generate_password_hash


DB_PATH = os.path.join(app.instance_path, "workflow.db")


def init_database():
    with app.app_context():

        # 1️⃣ حذف قاعدة البيانات القديمة (إن وُجدت)
        if os.path.exists(DB_PATH):
            print("🗑 Removing existing database...")
            os.remove(DB_PATH)

        # 2️⃣ إنشاء الجداول
        print("📦 Creating database tables...")
        db.create_all()

        # =========================
        # 3️⃣ إنشاء Admin User
        # =========================
        admin_email = "admin@pncecs.org"
        admin_password = "admin123"

        if not User.query.filter_by(email=admin_email).first():
            admin = User(
                email=admin_email,
                password_hash=generate_password_hash(admin_password),
                role="ADMIN",
                department_id=None
            )
            db.session.add(admin)
            print("👑 Admin user created")

        # =========================
        # 4️⃣ إنشاء User عادي
        # =========================
        user_email = "adham.pncecs@gmail.com"
        user_password = "user123"

        if not User.query.filter_by(email=user_email).first():
            user = User(
                email=user_email,
                password_hash=generate_password_hash(user_password),
                role="USER",
                department_id=None
            )
            db.session.add(user)
            print("👤 Normal user created")

        db.session.commit()

        print("===================================")
        print("✅ Database initialized successfully")
        print("===================================")
        print("Login credentials:")
        print(f"ADMIN  → {admin_email} / {admin_password}")
        print(f"USER   → {user_email} / {user_password}")


if __name__ == "__main__":
    init_database()
