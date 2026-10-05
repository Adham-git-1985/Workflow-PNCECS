import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from portal.timeclock_auto import (
    _HEARTBEAT_WRITE_INTERVAL_SECONDS,
    _record_heartbeat_if_due,
    _unavailable_source_delay,
    _update_last_error_if_changed,
)


class TimeclockAutoPerformanceTests(unittest.TestCase):
    def test_unchanged_error_does_not_write_again(self):
        with patch("portal.timeclock_auto._setting_set") as set_setting:
            result = _update_last_error_if_changed(
                "SOURCE_UNREACHABLE",
                "SOURCE_UNREACHABLE",
            )

        self.assertEqual(result, ("SOURCE_UNREACHABLE", False))
        set_setting.assert_not_called()

    def test_heartbeat_is_not_written_before_its_interval(self):
        now = datetime(2026, 10, 5, 8, 0, 0)
        last_heartbeat = now
        with patch("portal.timeclock_auto._setting_set") as set_setting:
            result = _record_heartbeat_if_due(
                last_heartbeat,
                now + timedelta(seconds=_HEARTBEAT_WRITE_INTERVAL_SECONDS - 1),
            )

        self.assertEqual(result, last_heartbeat)
        set_setting.assert_not_called()

    def test_unavailable_source_uses_a_bounded_backoff(self):
        self.assertEqual(_unavailable_source_delay(60), 300)
        self.assertEqual(_unavailable_source_delay(600), 600)


if __name__ == "__main__":
    unittest.main()
