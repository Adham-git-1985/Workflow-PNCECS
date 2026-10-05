import unittest
from datetime import datetime

from jobs.workflow_delay_summary_job import _seconds_until_next_run


class WorkflowDelaySummaryScheduleTests(unittest.TestCase):
    def test_sleeps_until_the_current_days_local_slot(self):
        # January in Asia/Jerusalem is UTC+2, so 06:20 UTC is 08:20 local.
        delay = _seconds_until_next_run(datetime(2026, 1, 12, 6, 20, 0))

        self.assertEqual(delay, 10 * 60)

    def test_schedules_tomorrow_after_the_daily_slot(self):
        # 06:31 UTC is 08:31 local, one minute after the 08:30 slot.
        delay = _seconds_until_next_run(datetime(2026, 1, 12, 6, 31, 0))

        self.assertEqual(delay, (23 * 60 + 59) * 60)


if __name__ == "__main__":
    unittest.main()
