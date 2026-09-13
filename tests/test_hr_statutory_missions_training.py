from datetime import date
import unittest

from portal.routes import _period_within_month_limit


class StatutoryMissionTrainingDateTests(unittest.TestCase):
    def test_official_mission_period_is_limited_to_one_inclusive_calendar_month(self):
        self.assertTrue(_period_within_month_limit(date(2026, 1, 1), date(2026, 1, 31), 1))
        self.assertFalse(_period_within_month_limit(date(2026, 1, 1), date(2026, 2, 1), 1))

    def test_training_period_is_limited_to_eight_months_and_handles_leap_year(self):
        self.assertTrue(_period_within_month_limit(date(2024, 2, 1), date(2024, 9, 30), 8))
        self.assertFalse(_period_within_month_limit(date(2024, 2, 1), date(2024, 10, 1), 8))

    def test_month_end_clamps_to_shorter_month(self):
        self.assertTrue(_period_within_month_limit(date(2024, 1, 31), date(2024, 2, 28), 1))
        self.assertFalse(_period_within_month_limit(date(2024, 1, 31), date(2024, 2, 29), 1))


if __name__ == "__main__":
    unittest.main()
