import argparse
import json
from datetime import datetime, timezone

from app import app
from extensions import db
from models import PortalPermissionPreset, Role, RolePermission, User, UserPermission
from sqlalchemy import func


ROLE_CODE = "GENERAL-SECRETARY"
ROLE_LABEL_AR = "الأمين العام"
ROLE_LABEL_EN = "General Secretary"

GENERAL_SECRETARY_KEYS = [
    "PORTAL_READ",
    "PORTAL_ADMIN_READ",
    "HR_READ",
    "HR_ATTENDANCE_READ",
    "HR_REQUESTS_READ",
    "HR_SS_READ",
    "HR_SS_CREATE",
    "HR_DOCS_READ",
    "STORE_READ",
    "PORTAL_CIRCULARS_MANAGE",
    "PORTAL_MEETINGS_MANAGE",
    "CORR_READ",
    "HR_REQUESTS_APPROVE",
    "HR_REQUESTS_VIEW_ALL",
    "HR_SS_APPROVE",
    "HR_DISCIPLINE_READ",
    "HR_PAYSLIP_VIEW",
    "HR_PERFORMANCE_READ",
    "HR_PERFORMANCE_SUBMIT",
    "HR_PERFORMANCE_MANAGE",
    "HR_PERFORMANCE_EXPORT",
    "HR_SYSTEM_EVALUATION_VIEW",
    "HR_EVALUATIONS_MANAGE",
    "HR_ATTENDANCE_EXPORT",
    "HR_REPORTS_VIEW",
    "HR_EMPLOYEE_READ",
    "HR_EMPLOYEE_ATTACHMENTS_MANAGE",
    "HR_ORGSTRUCTURE_MANAGE",
    "HR_ORG_DYNAMIC_GUIDE_VIEW",
    "TRANSPORT_READ",
    "TRANSPORT_CREATE",
    "TRANSPORT_TRACKING_READ",
    "PORTAL_REPORTS_READ",
    "PORTAL_REPORTS_EXPORT",
    "PORTAL_AUDIT_READ",
    "AUDIT_DASHBOARD_READ",
    "AUDIT_TIMELINE_READ",
    "WORKFLOW_NOTIFICATIONS_DASHBOARD_READ",
]


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def upsert_role() -> Role:
    role = Role.query.filter(func.lower(Role.code) == ROLE_CODE.lower()).first()
    if role is None:
        role = Role(code=ROLE_CODE, name_ar=ROLE_LABEL_AR, name_en=ROLE_LABEL_EN, is_active=True)
        db.session.add(role)
    else:
        role.name_ar = role.name_ar or ROLE_LABEL_AR
        role.name_en = role.name_en or ROLE_LABEL_EN
        role.is_active = True
    return role


def upsert_preset() -> PortalPermissionPreset:
    preset = PortalPermissionPreset.query.filter_by(code=ROLE_CODE).first()
    if preset is None:
        preset = PortalPermissionPreset(code=ROLE_CODE, category="main", sort_order=40, is_active=True)
        db.session.add(preset)
    preset.label = ROLE_LABEL_AR
    preset.category = "main"
    preset.keys_json = json.dumps(GENERAL_SECRETARY_KEYS, ensure_ascii=False)
    preset.is_active = True
    preset.updated_at = utcnow_naive()
    return preset


def grant_role_permissions() -> int:
    count = 0
    existing = {
        (row.permission or "").strip().upper()
        for row in RolePermission.query.filter(func.lower(RolePermission.role) == ROLE_CODE.lower()).all()
    }
    for key in GENERAL_SECRETARY_KEYS:
        if key not in existing:
            db.session.add(RolePermission(role=ROLE_CODE, permission=key))
            count += 1
    return count


def grant_user_permissions(user: User) -> int:
    count = 0
    existing = {
        (row.key or "").strip().upper()
        for row in UserPermission.query.filter_by(user_id=user.id, is_allowed=True).all()
    }
    for key in GENERAL_SECRETARY_KEYS:
        if key not in existing:
            db.session.add(UserPermission(user_id=user.id, key=key, is_allowed=True))
            count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="Grant General Secretary portal/Masar permissions.")
    parser.add_argument("--email", help="Grant permissions to this user email.")
    parser.add_argument("--user-id", type=int, help="Grant permissions to this user id.")
    parser.add_argument("--assign-role", action="store_true", help="Also set the user's role to GENERAL-SECRETARY.")
    parser.add_argument("--role-permissions", action="store_true", help="Grant permissions to the GENERAL-SECRETARY role.")
    parser.add_argument("--execute", action="store_true", help="Apply changes. Without this flag, only prints a dry run.")
    args = parser.parse_args()

    with app.app_context():
        user = None
        if args.user_id:
            user = User.query.get(args.user_id)
        elif args.email:
            user = User.query.filter(func.lower(User.email) == args.email.strip().lower()).first()

        if (args.user_id or args.email) and user is None:
            raise SystemExit("User not found.")

        upsert_role()
        upsert_preset()
        role_added = grant_role_permissions() if args.role_permissions else 0
        user_added = grant_user_permissions(user) if user else 0

        if user and args.assign_role:
            user.role = ROLE_CODE

        print(f"Preset: {ROLE_CODE} ({len(GENERAL_SECRETARY_KEYS)} keys)")
        print(f"Role permissions to add: {role_added}")
        print(f"User permissions to add: {user_added}")
        if user:
            print(f"User: {user.id} {user.email} role={ROLE_CODE if args.assign_role else user.role}")

        if args.execute:
            db.session.commit()
            print("Done.")
        else:
            db.session.rollback()
            print("Dry run only. Add --execute to apply changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
