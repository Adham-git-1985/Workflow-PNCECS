import json
import unittest
from datetime import datetime
from unittest.mock import patch

from flask import Flask
from flask_login import LoginManager, login_user, logout_user

from extensions import db
from models import EmployeeEvaluationRun, HREmployeeAchievement, HREmployeeAchievementApproval, User
from portal import portal_bp
from portal.routes import (
    _achievement_access_level,
    _ensure_achievement_approval_path,
    hr_achievement_review,
    hr_my_achievements,
)
from services.evaluation_service import (
    _score_with_achievement_bonus,
    compute_employee_evaluation,
    refresh_achievement_bonus_for_existing_runs,
)


class EmployeeAchievementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.config.update(
            SECRET_KEY="employee-achievement-tests",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(cls.app)
        LoginManager(cls.app)
        cls.app.register_blueprint(portal_bp)
        cls.context = cls.app.app_context()
        cls.context.push()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.context.pop()

    def setUp(self):
        db.drop_all()
        db.create_all()
        self.employee = User(
            email="achievement.employee@example.test",
            name="Achievement Employee",
            password_hash="x",
            role="SUPERADMIN",
        )
        db.session.add(self.employee)
        db.session.commit()

    def test_employee_submission_stays_pending_without_points(self):
        with self.app.test_request_context(
            "/portal/hr/me/achievements",
            method="POST",
            data={
                "achievement_type": "AWARD",
                "title": "جائزة أفضل مبادرة",
                "description": "مبادرة حسنت زمن الإنجاز.",
                "achieved_on": "2026-09-10",
                "issuer": "جهة مانحة",
                "evidence_reference": "كتاب رقم 15",
            },
        ):
            login_user(self.employee)
            response = hr_my_achievements()
            logout_user()

        self.assertEqual(response.status_code, 302)
        row = HREmployeeAchievement.query.one()
        self.assertEqual(row.status, "PENDING")
        self.assertEqual(row.evaluation_points, 0.0)
        run = compute_employee_evaluation(
            self.employee.id,
            "MONTHLY",
            2026,
            9,
            created_by_id=self.employee.id,
        )
        self.assertEqual(run.score_100, 0.0)

    def test_only_approved_achievements_apply_with_five_point_cap(self):
        db.session.add_all((
            HREmployeeAchievement(
                user_id=self.employee.id,
                achievement_type="AWARD",
                title="Award",
                achieved_on="2026-09-05",
                distinction_level="EXCEPTIONAL",
                evaluation_points=3.0,
                status="APPROVED",
            ),
            HREmployeeAchievement(
                user_id=self.employee.id,
                achievement_type="RESEARCH",
                title="Research",
                achieved_on="2026-09-08",
                distinction_level="EXCEPTIONAL",
                evaluation_points=3.0,
                status="APPROVED",
            ),
            HREmployeeAchievement(
                user_id=self.employee.id,
                achievement_type="DEVELOPMENT",
                title="Pending development",
                achieved_on="2026-09-09",
                distinction_level="EXCEPTIONAL",
                evaluation_points=3.0,
                status="PENDING",
            ),
        ))
        db.session.commit()

        run = compute_employee_evaluation(
            self.employee.id,
            "MONTHLY",
            2026,
            9,
            created_by_id=self.employee.id,
        )
        breakdown = json.loads(run.breakdown_json)

        self.assertEqual(run.score_100, 5.0)
        self.assertEqual(breakdown["achievement_bonus"]["count"], 2)
        self.assertEqual(breakdown["achievement_bonus"]["raw_points"], 6.0)
        self.assertEqual(breakdown["achievement_bonus"]["points"], 5.0)
        self.assertEqual(breakdown["score"]["base_score_100"], 0.0)

    def test_approval_refreshes_existing_evaluation(self):
        run = compute_employee_evaluation(
            self.employee.id,
            "MONTHLY",
            2026,
            9,
            created_by_id=self.employee.id,
        )
        self.assertEqual(run.score_100, 0.0)

        db.session.add(HREmployeeAchievement(
            user_id=self.employee.id,
            achievement_type="INNOVATION",
            title="Innovation",
            achieved_on="2026-09-12",
            distinction_level="SIGNIFICANT",
            evaluation_points=2.0,
            status="APPROVED",
            reviewed_at=datetime.utcnow(),
        ))
        db.session.commit()

        refreshed = refresh_achievement_bonus_for_existing_runs(
            self.employee.id,
            "2026-09-12",
        )
        updated = db.session.get(EmployeeEvaluationRun, run.id)

        self.assertEqual(refreshed, 1)
        self.assertEqual(updated.score_100, 2.0)
        self.assertIn("إنجازات مميزة: 1 (+2.0 نقطة)", updated.summary)

    def test_bonus_reports_only_points_applied_before_score_cap(self):
        bonus = {"points": 5.0}

        score_100, score_5 = _score_with_achievement_bonus(98.0, bonus)

        self.assertEqual(score_100, 100.0)
        self.assertEqual(score_5, 5.0)
        self.assertEqual(bonus["applied_points"], 2.0)
        self.assertEqual(bonus["limited_points"], 3.0)

    def test_achievement_path_sends_manager_copy_and_secretary_decision(self):
        manager = User(
            email="achievement.manager@example.test",
            name="Achievement Manager",
            password_hash="x",
            role="USER",
        )
        secretary = User(
            email="achievement.secretary@example.test",
            name="Achievement Secretary",
            password_hash="x",
            role="SUPERADMIN",
        )
        db.session.add_all((manager, secretary))
        db.session.flush()

        row = HREmployeeAchievement(
            user_id=self.employee.id,
            achievement_type="AWARD",
            title="Path test",
            achieved_on="2026-09-10",
            status="PENDING",
            distinction_level="NOTABLE",
            evaluation_points=0.0,
        )
        db.session.add(row)
        db.session.flush()

        with patch("portal.routes._achievement_manager_ids", return_value=[manager.id]), \
             patch("portal.routes.secretary_general_user_ids", return_value=[secretary.id]), \
             patch("portal.routes._achievement_privileged", return_value=False):
            _ensure_achievement_approval_path(row)

            steps = {(step.approver_role, step.approver_user_id): step for step in row.approval_steps}
            self.assertEqual(steps[("DIRECT_MANAGER", manager.id)].status, "VIEW_ONLY")
            self.assertEqual(steps[("SECRETARY_GENERAL", secretary.id)].status, "PENDING")
            self.assertEqual(_achievement_access_level(row, manager), "observer")
            self.assertEqual(_achievement_access_level(row, secretary), "secretary_general")

        db.session.commit()

        with patch("portal.routes._achievement_manager_ids", return_value=[manager.id]), \
             patch("portal.routes.secretary_general_user_ids", return_value=[secretary.id]), \
             patch("portal.routes._achievement_privileged", return_value=False):
            with self.app.test_request_context(
                f"/portal/hr/achievements/{row.id}/review",
                method="POST",
                data={
                    "action": "APPROVE",
                    "distinction_level": "SIGNIFICANT",
                    "decision_note": "Approved by the Secretary General.",
                },
            ):
                login_user(secretary)
                response = hr_achievement_review(row.id)
                logout_user()

        self.assertEqual(response.status_code, 302)
        db.session.refresh(row)
        self.assertEqual(row.status, "APPROVED")
        self.assertEqual(row.reviewed_by_id, secretary.id)
        steps = {(step.approver_role, step.approver_user_id): step for step in row.approval_steps}
        self.assertEqual(steps[("DIRECT_MANAGER", manager.id)].status, "VIEW_ONLY")
        self.assertEqual(steps[("SECRETARY_GENERAL", secretary.id)].status, "APPROVED")

    def test_pending_legacy_path_is_reversed_without_reopening_completed_rows(self):
        manager = User(
            email="legacy.manager@example.test",
            name="Legacy Manager",
            password_hash="x",
            role="USER",
        )
        secretary = User(
            email="legacy.secretary@example.test",
            name="Legacy Secretary",
            password_hash="x",
            role="USER",
        )
        db.session.add_all((manager, secretary))
        db.session.flush()

        row = HREmployeeAchievement(
            user_id=self.employee.id,
            achievement_type="AWARD",
            title="Legacy path test",
            achieved_on="2026-09-11",
            status="PENDING",
            distinction_level="NOTABLE",
            evaluation_points=0.0,
        )
        db.session.add(row)
        db.session.flush()
        db.session.add_all((
            HREmployeeAchievementApproval(
                achievement_id=row.id,
                approver_user_id=manager.id,
                approver_role="DIRECT_MANAGER",
                status="PENDING",
            ),
            HREmployeeAchievementApproval(
                achievement_id=row.id,
                approver_user_id=secretary.id,
                approver_role="SECRETARY_GENERAL",
                status="VIEW_ONLY",
            ),
        ))
        db.session.commit()

        with patch("portal.routes._achievement_manager_ids", return_value=[manager.id]), \
             patch("portal.routes.secretary_general_user_ids", return_value=[secretary.id]):
            _ensure_achievement_approval_path(row)

        steps = {(step.approver_role, step.approver_user_id): step for step in row.approval_steps}
        self.assertEqual(steps[("DIRECT_MANAGER", manager.id)].status, "VIEW_ONLY")
        self.assertEqual(steps[("SECRETARY_GENERAL", secretary.id)].status, "PENDING")


if __name__ == "__main__":
    unittest.main()
