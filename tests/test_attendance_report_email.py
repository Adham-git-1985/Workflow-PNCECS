import unittest
from datetime import datetime, time
from unittest.mock import patch

from sqlalchemy import create_engine, select

from models import HRAttendanceEmailReportDelivery
import services.attendance_report_email as attendance_email


class AttendanceReportEmailTests(unittest.TestCase):
    def test_multiple_send_times_are_normalized_and_legacy_time_is_supported(self):
        self.assertEqual(
            [time(9, 30), time(15, 30)],
            attendance_email._parse_hhmm_list(["15:30", "09:30", "15:30"]),
        )

        def legacy_setting(key, default=None):
            if key == attendance_email.SETTING_KEYS["send_time"]:
                return "15:30"
            return default

        with patch.object(attendance_email, "_setting", side_effect=legacy_setting):
            config = attendance_email.get_report_email_config()
        self.assertEqual(config["send_times_list"], ["15:30"])
        self.assertEqual(config["send_time"], "15:30")

    def test_due_cycle_runs_only_the_current_slot_once(self):
        config = {
            "enabled": True,
            "send_times_list": ["09:30", "15:30"],
            "send_time": "09:30",
            "frequency": "DAILY",
            "skip_weekly_holidays": False,
            "skip_official_holidays": False,
            "excluded_dates_list": [],
            "report_day_mode": "TODAY",
        }
        sent = []

        def send_slot(now, current_config, scheduled_time, *, force=False):
            sent.append(scheduled_time.strftime("%H:%M"))
            return {"status": "sent", "scheduled_time": sent[-1]}

        with (
            patch.object(attendance_email, "get_report_email_config", return_value=config),
            patch.object(attendance_email, "_schedule_is_due", return_value=True),
            patch.object(attendance_email, "_send_day_is_excluded", return_value=False),
            patch.object(attendance_email, "_run_attendance_report_email_slot", side_effect=send_slot),
        ):
            result = attendance_email.run_attendance_report_email_cycle(
                datetime(2026, 9, 23, 15, 30)
            )

        self.assertEqual(sent, ["15:30"])
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["scheduled_time"], "15:30")

    def test_cycle_does_not_catch_up_a_missed_slot(self):
        config = {
            "enabled": True,
            "send_times_list": ["09:30", "15:30"],
            "send_time": "09:30",
            "frequency": "DAILY",
            "skip_weekly_holidays": False,
            "skip_official_holidays": False,
            "excluded_dates_list": [],
            "report_day_mode": "TODAY",
        }
        with (
            patch.object(attendance_email, "get_report_email_config", return_value=config),
            patch.object(attendance_email, "_schedule_is_due", return_value=True),
            patch.object(attendance_email, "_send_day_is_excluded", return_value=False),
            patch.object(attendance_email, "_run_attendance_report_email_slot") as send_slot,
        ):
            result = attendance_email.run_attendance_report_email_cycle(
                datetime(2026, 9, 23, 15, 31)
            )

        self.assertEqual(result["status"], "not_due")
        send_slot.assert_not_called()

    def test_cycle_records_not_due_state_for_diagnostics(self):
        config = {
            "enabled": True,
            "send_times_list": ["09:30", "15:30"],
            "send_time": "09:30",
            "frequency": "DAILY",
            "skip_weekly_holidays": False,
            "skip_official_holidays": False,
            "excluded_dates_list": [],
            "report_day_mode": "TODAY",
        }
        with (
            patch.object(attendance_email, "get_report_email_config", return_value=config),
            patch.object(attendance_email, "_record_scheduler_state") as record_state,
        ):
            result = attendance_email.run_attendance_report_email_cycle(
                datetime(2026, 9, 23, 8, 0)
            )

        self.assertEqual(result["status"], "not_due")
        record_state.assert_called_once()
        self.assertEqual(record_state.call_args.args[1], "NOT_DUE")

    def test_email_content_contains_departures_section_and_slot(self):
        data = {
            "report_day": datetime(2026, 9, 23).date(),
            "scheduled_time": "15:30",
            "overall": [],
            "late": [],
            "absence": [],
            "departures": [{
                "employee_no": "1001",
                "name": "موظف الاختبار",
                "departure_type": "مغادرة شخصية",
                "permission_name": "مغادرة عادية",
                "source": "ساعة الدوام",
                "time_range": "ساعة الدوام: من 10:00 إلى 10:30",
                "minutes": 30,
                "status": "مسجلة",
            }],
        }
        subject, text_body, html_body = attendance_email.build_email_content(data)
        self.assertIn("15:30", subject)
        self.assertIn("قائمة المغادرات", text_body)
        self.assertIn("موظف الاختبار", html_body)

    def test_delivery_marker_allows_two_slots_on_the_same_day(self):
        engine = create_engine("sqlite:///:memory:")
        table = HRAttendanceEmailReportDelivery.__table__
        table.create(engine)
        with engine.begin() as connection:
            connection.execute(table.insert(), [
                {"run_date": "2026-09-23", "scheduled_time": "09:30", "report_day": "2026-09-23"},
                {"run_date": "2026-09-23", "scheduled_time": "15:30", "report_day": "2026-09-23"},
            ])
            rows = connection.execute(select(table.c.run_date, table.c.scheduled_time)).all()
        self.assertEqual(
            {("2026-09-23", "09:30"), ("2026-09-23", "15:30")},
            {(row.run_date, row.scheduled_time) for row in rows},
        )
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
