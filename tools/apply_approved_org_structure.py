from __future__ import annotations

import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app import app
from extensions import db
from utils.approved_org_structure import apply_approved_org_structure


def main() -> int:
    with app.app_context():
        try:
            result = apply_approved_org_structure(deactivate_unlisted=True, lock_legacy=True)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

    output = {key: value for key, value in result.items() if key != "nodes_by_key"}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
