"""Production network entry point for the PNCECS application server."""

import os

from waitress import serve

from app import app
from jobs.backup_job import start_automatic_backup_job
from jobs.workflow_task_email_job import start_workflow_task_email_job


if __name__ == "__main__":
    start_automatic_backup_job(app)
    start_workflow_task_email_job(app)
    serve(
        app,
        host=os.getenv("APP_HOST", "10.10.10.204"),
        port=int(os.getenv("APP_PORT", "5000")),
        threads=int(os.getenv("APP_SERVER_THREADS", "8")),
    )
