import unittest
from unittest.mock import patch

from portal.routes import _timeclock_read_incremental, _timeclock_resolve_source_file


class TimeclockSourceResolutionTests(unittest.TestCase):
    def test_unreachable_network_source_is_treated_as_unavailable(self):
        with patch("portal.routes.Path") as path_class:
            path_class.return_value.exists.side_effect = OSError("network share unavailable")

            self.assertIsNone(
                _timeclock_resolve_source_file(r"\\10.10.10.200\Data")
            )

    def test_network_read_failure_becomes_file_not_found(self):
        with patch("portal.routes.Path") as path_class:
            path_class.return_value.exists.side_effect = OSError("network share unavailable")

            with self.assertRaises(FileNotFoundError):
                _timeclock_read_incremental(r"\\10.10.10.200\Data\20260831.CSV", None, True)


if __name__ == "__main__":
    unittest.main()
