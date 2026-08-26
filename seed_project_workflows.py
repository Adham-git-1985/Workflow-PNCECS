"""Create the approved predefined workflows for project management.

Safe to run more than once::

    python seed_project_workflows.py
"""

from app import app
from extensions import db
from workflow.project_workflows import upsert_project_workflows


def seed() -> list[dict]:
    with app.app_context():
        try:
            results = upsert_project_workflows()
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

        for result in results:
            print(
                f"[project-workflow] {result['template_name']} "
                f"(#{result['template_id']}, {result['step_count']} steps): "
                f"{result['description']}"
            )
        return results


if __name__ == "__main__":
    seed()
