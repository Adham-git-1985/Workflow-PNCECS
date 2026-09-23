import unittest
from unittest.mock import patch

from flask import Flask

from jobs import workflow_task_email_job


class WorkflowTaskEmailJobTests(unittest.TestCase):
    def test_one_email_step_failure_does_not_block_attendance_step(self):
        app = Flask(__name__)
        calls = []

        def failing_step():
            calls.append("workflow")
            raise RuntimeError("stale schema")

        def attendance_step():
            calls.append("attendance")
            return {"status": "not_due"}

        with patch.object(app.logger, "exception"):
            self.assertIsNone(
                workflow_task_email_job._run_job_step(
                    app, "workflow task", failing_step
                )
            )
        self.assertEqual(
            workflow_task_email_job._run_job_step(
                app, "attendance report", attendance_step
            ),
            {"status": "not_due"},
        )
        self.assertEqual(calls, ["workflow", "attendance"])


if __name__ == "__main__":
    unittest.main()
