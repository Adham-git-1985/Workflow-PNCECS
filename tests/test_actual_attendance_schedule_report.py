import unittest
from datetime import date, timedelta

from flask import Flask

from extensions import db
from models import (
    EmployeeFile,
    HRAttendanceScheduleDay,
    HRAttendanceSchedulePlan,
    User,
)
from workflow.attendance_reports import (
    _classify_group_from_context,
    build_actual_attendance_report,
)


class ActualAttendanceScheduleReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask("actual-attendance-schedule-report-tests")
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY="actual-attendance-schedule-report-tests",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(cls.app)
        cls.context = cls.app.app_context()
        cls.context.push()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.context.pop()

    def setUp(self):
        db.session.remove()
        db.drop_all()
        db.create_all()

    def _add_user(self, name="Schedule Employee"):
        user = User(
            email=f"{name.replace(' ', '.').lower()}@example.test",
            name=name,
            password_hash="not-used-in-test",
            role="EMPLOYEE",
        )
        db.session.add(user)
        db.session.flush()
        db.session.add(EmployeeFile(user_id=user.id, full_name_quad=name))
        return user

    def _add_plan(self, user, start_day, *, off_offsets=(), day_types=None):
        day_types = day_types or {}
        plan = HRAttendanceSchedulePlan(
            user_id=user.id,
            period_start=start_day.isoformat(),
            period_end=(start_day + timedelta(days=6)).isoformat(),
            version_no=1,
            status="FINAL_APPROVED",
            request_type="BASELINE",
        )
        db.session.add(plan)
        db.session.flush()
        for offset in range(7):
            work_day = start_day + timedelta(days=offset)
            db.session.add(
                HRAttendanceScheduleDay(
                    plan_id=plan.id,
                    work_date=work_day.isoformat(),
                    day_type=day_types.get(
                        offset,
                        "OFF" if offset in off_offsets else "WORK",
                    ),
                )
            )
        return plan

    def test_future_days_use_the_published_schedule_without_daily_attendance_rows(self):
        user = self._add_user()
        self._add_plan(user, date(2026, 10, 18))
        db.session.commit()

        report = build_actual_attendance_report("2026-10-18")

        self.assertEqual(report["total_counts"], [1, 1, 1, 1, 1])
        self.assertTrue(report["has_data"])
        self.assertEqual(
            report["groups"][-1]["days"][3]["names"],
            ["Schedule Employee"],
        )

    def test_remote_days_do_not_appear_as_in_office_attendance(self):
        user = self._add_user()
        self._add_plan(
            user,
            date(2026, 10, 18),
            day_types={1: "REMOTE", 3: "REMOTE"},
        )
        db.session.commit()

        report = build_actual_attendance_report("2026-10-18")

        self.assertEqual(report["total_counts"], [1, 0, 1, 0, 1])

    def test_plan_continues_after_period_end_until_a_later_plan_starts(self):
        user = self._add_user()
        self._add_plan(user, date(2026, 10, 18))
        db.session.commit()

        continuing_report = build_actual_attendance_report("2026-10-25")
        self.assertEqual(continuing_report["total_counts"], [1, 1, 1, 1, 1])

        replacement = self._add_plan(user, date(2026, 11, 1), off_offsets=(2,))
        replacement.version_no = 1
        db.session.commit()

        replaced_report = build_actual_attendance_report("2026-11-01")
        self.assertEqual(replaced_report["total_counts"], [1, 1, 0, 1, 1])

    def test_current_hierarchy_labels_map_the_report_groups(self):
        self.assertEqual(
            _classify_group_from_context(
                org_context=("قسم الشؤون الادارية", "دائرة الموارد البشرية", "الإدارة العامة للشؤون الإدارية والمالية"),
            ),
            "finance",
        )
        self.assertEqual(
            _classify_group_from_context(
                org_context=("مستشار لشؤون 48", "الامين العام"),
            ),
            "advisor_48",
        )
        self.assertEqual(
            _classify_group_from_context(
                org_context=("دائرة التدقيق والمتابعة من منظور النوع الاجتماعي",),
            ),
            "social",
        )


if __name__ == "__main__":
    unittest.main()
