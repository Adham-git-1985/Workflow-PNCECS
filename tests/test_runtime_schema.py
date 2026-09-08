import unittest

from utils.runtime_schema import (
    is_flask_db_command,
    should_run_runtime_schema_sync,
)


class RuntimeSchemaSyncTests(unittest.TestCase):
    def test_detects_flask_migrate_command(self):
        self.assertTrue(
            is_flask_db_command(["--app", "app", "db", "upgrade"])
        )

    def test_does_not_treat_regular_flask_command_as_migration(self):
        self.assertFalse(
            is_flask_db_command(["--app", "app", "run"])
        )

    def test_skips_runtime_sync_for_flask_migrate(self):
        self.assertFalse(
            should_run_runtime_schema_sync(
                ["--app", "app", "db", "upgrade"],
                {},
            )
        )

    def test_explicit_environment_flag_still_skips_runtime_sync(self):
        self.assertFalse(
            should_run_runtime_schema_sync(
                ["--app", "app", "run"],
                {"SKIP_RUNTIME_SCHEMA": "1"},
            )
        )

    def test_regular_application_startup_runs_runtime_sync(self):
        self.assertTrue(
            should_run_runtime_schema_sync(
                ["--app", "app", "run"],
                {},
            )
        )


if __name__ == "__main__":
    unittest.main()
