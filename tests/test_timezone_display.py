import unittest
from datetime import date, datetime

from flask import Flask

from utils.timezone import format_local_datetime, local_day_start_utc, to_local_time


class TimezoneDisplayTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["APP_TIMEZONE"] = "Asia/Jerusalem"

    def test_utc_timestamps_render_in_jerusalem_summer_time(self):
        with self.app.app_context():
            value = datetime(2026, 9, 7, 9, 45)

            self.assertEqual(format_local_datetime(value), "2026-09-07 12:45")
            self.assertEqual(to_local_time(value).utcoffset().total_seconds(), 10_800)

    def test_utc_timestamps_render_in_jerusalem_winter_time(self):
        with self.app.app_context():
            value = datetime(2026, 1, 7, 9, 45)

            self.assertEqual(format_local_datetime(value), "2026-01-07 11:45")
            self.assertEqual(to_local_time(value).utcoffset().total_seconds(), 7_200)

    def test_notification_day_filter_uses_local_calendar_boundaries(self):
        with self.app.app_context():
            self.assertEqual(
                local_day_start_utc(date(2026, 9, 7)),
                datetime(2026, 9, 6, 21, 0),
            )


if __name__ == "__main__":
    unittest.main()
