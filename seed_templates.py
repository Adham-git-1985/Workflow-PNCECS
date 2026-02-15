# seed_templates.py
"""Seed default workflow templates.

This script is SAFE to re-run.
It ensures the demo template exists and that its first step routes to SUPER_ADMIN.
"""

from app import app
from extensions import db
from models import (
    User,
    WorkflowTemplate,
    WorkflowTemplateStep,
    WorkflowInstance,
    WorkflowInstanceStep,
)

def seed():
    with app.app_context():
        # اجلب SUPER_ADMIN (موجود عندك بالـ init_db)
        super_admin = User.query.filter_by(email="superadmin@pncecs.org").first()
        if not super_admin:
            print("❌ SUPER_ADMIN user not found. Run init_db.py first.")
            return

        demo_name = "مسار تجريبي (Super Admin Approval)"

        # ✅ Upsert template
        t = WorkflowTemplate.query.filter_by(name=demo_name).first()
        if not t:
            # لو كان عندك نسخة قديمة بالاسم القديم، حدّثها بدل ما تنشئ تكرار
            legacy = WorkflowTemplate.query.filter_by(name="مسار تجريبي (Admin Approval)").first()
            if legacy:
                legacy.name = demo_name
                t = legacy
            else:
                t = WorkflowTemplate(
                    name=demo_name,
                    is_active=True,
                    created_by_id=super_admin.id,
                    sla_days_default=3
                )
                db.session.add(t)
                db.session.flush()

        # ✅ Ensure step 1 routes to SUPER_ADMIN
        step1 = WorkflowTemplateStep.query.filter_by(template_id=t.id, step_order=1).first()
        if not step1:
            step1 = WorkflowTemplateStep(
                template_id=t.id,
                step_order=1,
                approver_kind="ROLE",
                approver_role="SUPER_ADMIN",
                sla_days=2
            )
            db.session.add(step1)
        else:
            step1.approver_kind = "ROLE"
            step1.approver_role = "SUPER_ADMIN"
            step1.sla_days = step1.sla_days or 2
            db.session.add(step1)

        db.session.commit()

        # ✅ Fix already-running demo instances (if any)
        try:
            inst_ids = [
                i.id for i in WorkflowInstance.query.filter_by(template_id=t.id).all()
            ]
            if inst_ids:
                (WorkflowInstanceStep.query
                 .filter(WorkflowInstanceStep.instance_id.in_(inst_ids))
                 .filter(WorkflowInstanceStep.step_order == 1)
                 .filter(WorkflowInstanceStep.status == "PENDING")
                 .update({WorkflowInstanceStep.approver_role: "SUPER_ADMIN"}, synchronize_session=False))
                db.session.commit()
        except Exception:
            db.session.rollback()

        print("✅ Demo template is ready: step #1 → SUPER_ADMIN")

if __name__ == "__main__":
    seed()
