from __future__ import annotations

from collections import defaultdict, deque
from threading import Lock
import time

from flask import current_app, jsonify, request
from flask_login import current_user, login_required
from flask_wtf.csrf import validate_csrf
from wtforms.validators import ValidationError

from . import assistant_bp
from .project_knowledge import index_stats, internal_knowledge_allowed, rebuild_project_index
from .service import answer


_request_times: dict[int, deque[float]] = defaultdict(deque)
_request_times_lock = Lock()


def _rate_limit_allows(user_id: int) -> bool:
    now = time.monotonic()
    window_seconds = int(current_app.config.get("ASSISTANT_RATE_WINDOW_SECONDS", 60))
    limit = int(current_app.config.get("ASSISTANT_RATE_LIMIT", 20))
    with _request_times_lock:
        timestamps = _request_times[user_id]
        while timestamps and now - timestamps[0] >= window_seconds:
            timestamps.popleft()
        if len(timestamps) >= limit:
            return False
        timestamps.append(now)
    return True


def _clean_history(raw_history) -> list[dict[str, str]]:
    if not isinstance(raw_history, list):
        return []
    history: list[dict[str, str]] = []
    for item in raw_history[-8:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = str(item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        history.append({"role": role, "content": content[:1600]})
    return history


def _clean_context(raw_context) -> dict[str, str]:
    if not isinstance(raw_context, dict):
        return {}
    return {
        "path": str(raw_context.get("path") or "")[:240],
        "title": str(raw_context.get("title") or "")[:160],
    }


@assistant_bp.route("/knowledge/status", methods=["GET"])
@login_required
def knowledge_status():
    """Expose non-sensitive index coverage to administrators."""
    if not internal_knowledge_allowed(current_user):
        return jsonify({"error": "forbidden", "message": "معرفة المشروع الداخلية متاحة للإدارة فقط."}), 403
    response = jsonify({"status": "ready", "index": index_stats()})
    response.headers["Cache-Control"] = "no-store"
    return response


@assistant_bp.route("/knowledge/reindex", methods=["POST"])
@login_required
def knowledge_reindex():
    """Refresh Aref's in-process source and file index."""
    if not internal_knowledge_allowed(current_user):
        return jsonify({"error": "forbidden", "message": "إعادة فهرسة المشروع متاحة للإدارة فقط."}), 403
    try:
        validate_csrf(request.headers.get("X-CSRFToken", ""))
    except ValidationError:
        return jsonify({"error": "csrf", "message": "انتهت صلاحية الجلسة. حدّث الصفحة وحاول مجددًا."}), 400
    stats = rebuild_project_index()
    current_app.logger.info("Aref project index rebuilt by user_id=%s stats=%s", current_user.id, stats)
    response = jsonify({"status": "rebuilt", "index": stats})
    response.headers["Cache-Control"] = "no-store"
    return response


@assistant_bp.route("/chat", methods=["POST"])
@login_required
def chat():
    try:
        validate_csrf(request.headers.get("X-CSRFToken", ""))
    except ValidationError:
        return jsonify({"error": "csrf", "message": "انتهت صلاحية الجلسة. حدّث الصفحة وحاول مجددًا."}), 400

    if not request.is_json:
        return jsonify({"error": "invalid_content_type", "message": "يجب إرسال الطلب بصيغة JSON."}), 415

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "invalid_json", "message": "تعذر قراءة رسالة عارف."}), 400

    message = str(payload.get("message") or "").strip()
    max_chars = int(current_app.config.get("ASSISTANT_MAX_MESSAGE_CHARS", 1200))
    if not message:
        return jsonify({"error": "empty_message", "message": "اكتب سؤالك أولًا."}), 400
    if len(message) > max_chars:
        return jsonify(
            {
                "error": "message_too_long",
                "message": f"اختصر السؤال إلى {max_chars} حرفًا أو أقل.",
            }
        ), 400

    if not _rate_limit_allows(int(current_user.id)):
        return jsonify(
            {
                "error": "rate_limited",
                "message": "أرسلت رسائل كثيرة خلال وقت قصير. انتظر قليلًا ثم حاول مجددًا.",
            }
        ), 429

    try:
        result = answer(
            current_user,
            message,
            history=_clean_history(payload.get("history")),
            context=_clean_context(payload.get("context")),
        )
        current_app.logger.info(
            "Aref answered user_id=%s access=%s intents=%s",
            current_user.id,
            result.get("access_level", "employee"),
            ",".join(result.get("intents") or []) or "guidance",
        )
    except Exception:
        current_app.logger.exception("Assistant request failed for user_id=%s", current_user.id)
        return jsonify(
            {
                "error": "assistant_failed",
                "message": "تعذر تشغيل عارف الآن. جرّب مرة أخرى بعد قليل.",
            }
        ), 500

    response = jsonify(result)
    response.headers["Cache-Control"] = "no-store"
    return response
