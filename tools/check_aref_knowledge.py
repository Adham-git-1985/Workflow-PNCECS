"""Smoke-test Aref's local knowledge against the current application database."""

from __future__ import annotations

from pathlib import Path
import argparse
import os
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# A smoke test must not run app.py's best-effort schema mutations.
os.environ.setdefault("SKIP_RUNTIME_SCHEMA", "1")
os.environ.setdefault("ASSISTANT_AI_ENABLED", "0")

from app import app  # noqa: E402
from assistant.service import answer  # noqa: E402
from models import User  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Aref's local project/database retrieval.")
    parser.add_argument("--email", default="superadmin@pncecs.org")
    parser.add_argument("--question", default="اشرح هيكلية المشروع وقاعدة البيانات")
    args = parser.parse_args()

    app.config["ASSISTANT_AI_ENABLED"] = "0"
    with app.test_request_context("/"):
        user = User.query.filter_by(email=args.email).first()
        if not user:
            print(f"User not found: {args.email}", file=sys.stderr)
            return 2
        result = answer(user, args.question, context={"title": "فحص عارف", "path": "/"})
        print(f"mode={result.get('mode')}")
        print(f"access={result.get('access_level')}")
        print(f"intents={','.join(result.get('intents') or [])}")
        print(f"sources={len(result.get('sources') or [])}")
        print(f"index={result.get('index_stats')}")
        print("--- reply ---")
        print(result.get("reply") or "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
