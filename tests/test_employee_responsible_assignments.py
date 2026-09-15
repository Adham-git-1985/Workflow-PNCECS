import unittest
from pathlib import Path

from flask import Flask
from flask_login import LoginManager

from extensions import db
from models import (
    AuditLog,
    EmployeeFile,
    EmployeeResponsibleAssignment,
    OrgNode,
    OrgNodeManager,
    OrgNodeType,
    User,
)
from portal import portal_bp
from portal.routes import _build_dynamic_employee_detail
from services.hr_request_workflow import resolve_direct_manager, resolve_responsible_managers
from workflow.dynamic_paths import requester_dynamic_manager_options


class EmployeeResponsibleAssignmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        project_root = Path(__file__).resolve().parents[1]
        cls.app = Flask(
            __name__,
            template_folder=str(project_root / "templates"),
        )
        cls.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            SECRET_KEY="employee-responsible-assignment-test",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(cls.app)
        login_manager = LoginManager()
        login_manager.init_app(cls.app)

        @login_manager.user_loader
        def load_user(user_id):
            return db.session.get(User, int(user_id))

        cls.app.register_blueprint(portal_bp)
        cls.context = cls.app.app_context()
        cls.context.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.context.pop()

    def setUp(self):
        db.session.remove()
        db.drop_all()
        db.create_all()

        self.actor = User(
            email="hr-responsible-actor@example.test",
            name="HR Actor",
            password_hash="x",
            role="SUPER_ADMIN",
        )
        self.employee = User(
            email="responsibility-employee@example.test",
            name="Employee Under Assignment",
            password_hash="x",
            role="EMPLOYEE",
        )
        self.legacy_manager = User(
            email="legacy-manager@example.test",
            name="Legacy Manager",
            password_hash="x",
            role="EMPLOYEE",
        )
        self.first_manager = User(
            email="assigned-manager-one@example.test",
            name="Assigned Manager One",
            password_hash="x",
            role="EMPLOYEE",
        )
        self.second_manager = User(
            email="assigned-manager-two@example.test",
            name="Assigned Manager Two",
            password_hash="x",
            role="EMPLOYEE",
        )
        db.session.add_all((
            self.actor,
            self.employee,
            self.legacy_manager,
            self.first_manager,
            self.second_manager,
        ))
        db.session.flush()
        db.session.add(EmployeeFile(
            user_id=self.employee.id,
            direct_manager_user_id=self.legacy_manager.id,
        ))
        db.session.commit()
        self.client = self.app.test_client()

    def _login(self, user_id):
        with self.client.session_transaction() as session:
            session.clear()
            session["_user_id"] = str(user_id)
            session["_fresh"] = True

    def test_explicit_assignments_override_legacy_manager_and_support_dynamic_paths(self):
        db.session.add_all((
            EmployeeResponsibleAssignment(
                employee_user_id=self.employee.id,
                responsible_user_id=self.first_manager.id,
                reason="تكليف مؤقت للمراجعة",
            ),
            EmployeeResponsibleAssignment(
                employee_user_id=self.employee.id,
                responsible_user_id=self.second_manager.id,
                reason="تغطية بديلة معتمدة",
            ),
        ))
        db.session.commit()

        self.assertEqual(resolve_direct_manager(self.employee.id).id, self.first_manager.id)
        self.assertEqual(
            [manager.id for manager in resolve_responsible_managers(self.employee.id)],
            [self.first_manager.id, self.second_manager.id],
        )
        self.assertEqual(
            [option["user_id"] for option in requester_dynamic_manager_options(self.employee)],
            [self.first_manager.id, self.second_manager.id],
        )

    def test_org_detail_builds_location_and_manager_chain(self):
        root_type = OrgNodeType(
            code="ORGANIZATION",
            name_ar="مؤسسة",
            sort_order=10,
            is_active=True,
            show_in_chart=True,
            show_in_routes=True,
            allow_in_approvals=True,
        )
        department_type = OrgNodeType(
            code="DEPARTMENT",
            name_ar="دائرة",
            sort_order=40,
            is_active=True,
            show_in_chart=True,
            show_in_routes=True,
            allow_in_approvals=True,
        )
        db.session.add_all((root_type, department_type))
        db.session.flush()
        root = OrgNode(type_id=root_type.id, name_ar="المؤسسة الأم", code="ORG-1")
        department = OrgNode(
            type_id=department_type.id,
            parent=root,
            name_ar="دائرة الموارد البشرية",
            code="HR-1",
        )
        db.session.add_all((root, department))
        db.session.flush()
        db.session.add_all((
            OrgNodeManager(node_id=department.id, manager_user_id=self.first_manager.id),
            OrgNodeManager(node_id=root.id, manager_user_id=self.second_manager.id),
        ))
        db.session.commit()

        detail = _build_dynamic_employee_detail(
            self.employee,
            department,
            {root.id: root, department.id: department},
            {department.id: OrgNodeManager.query.filter_by(node_id=department.id).one(),
             root.id: OrgNodeManager.query.filter_by(node_id=root.id).one()},
        )

        self.assertEqual(
            [item["name"] for item in detail["location_nodes"]],
            ["المؤسسة الأم", "دائرة الموارد البشرية"],
        )
        self.assertEqual(
            [item["manager"].id for item in detail["manager_chain"] if item["manager"]],
            [self.legacy_manager.id, self.first_manager.id, self.second_manager.id],
        )

    def test_save_route_audits_add_remove_and_reason_update(self):
        self._login(self.actor.id)
        response = self.client.post(
            "/portal/hr/org-structure/employee-responsibles",
            data={
                "employee_user_id": str(self.employee.id),
                "responsible_user_ids": [
                    str(self.first_manager.id),
                    str(self.second_manager.id),
                ],
                "reason": "توزيع مسؤولية مؤقتة بين مديرين",
                "q": "Employee Under Assignment",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            EmployeeResponsibleAssignment.query.filter_by(
                employee_user_id=self.employee.id,
                is_active=True,
            ).count(),
            2,
        )
        self.assertEqual(
            AuditLog.query.filter(
                AuditLog.target_type == "EMPLOYEE_RESPONSIBILITY",
                AuditLog.action == "HR_EMPLOYEE_RESPONSIBLE_ADD",
            ).count(),
            2,
        )

        response = self.client.post(
            "/portal/hr/org-structure/employee-responsibles",
            data={
                "employee_user_id": str(self.employee.id),
                "responsible_user_ids": str(self.second_manager.id),
                "reason": "تحديث سبب التغطية الإدارية",
            },
        )
        self.assertEqual(response.status_code, 302)
        first_row = EmployeeResponsibleAssignment.query.filter_by(
            employee_user_id=self.employee.id,
            responsible_user_id=self.first_manager.id,
        ).one()
        second_row = EmployeeResponsibleAssignment.query.filter_by(
            employee_user_id=self.employee.id,
            responsible_user_id=self.second_manager.id,
        ).one()
        self.assertFalse(first_row.is_active)
        self.assertTrue(second_row.is_active)
        self.assertEqual(second_row.reason, "تحديث سبب التغطية الإدارية")
        self.assertEqual(
            [manager.id for manager in resolve_responsible_managers(self.employee.id)],
            [self.second_manager.id],
        )
        actions = {
            row.action
            for row in AuditLog.query.filter_by(target_type="EMPLOYEE_RESPONSIBILITY").all()
        }
        self.assertTrue({
            "HR_EMPLOYEE_RESPONSIBLE_ADD",
            "HR_EMPLOYEE_RESPONSIBLE_REMOVE",
            "HR_EMPLOYEE_RESPONSIBLE_UPDATE",
        }.issubset(actions))


if __name__ == "__main__":
    unittest.main()
