import json
import unittest
from datetime import datetime

from flask import Flask
from flask_login import LoginManager, login_user, logout_user

from extensions import db
from models import EmployeeEvaluationRun, HREmployeeAchievement, User
from portal import portal_bp
from portal.routes import hr_my_achievements
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


if __name__ == "__main__":
    unittest.main()
