from __future__ import annotations

from collections import defaultdict, deque
from threading import Lock
import time

from flask import current_app, jsonify, request
from flask_login import current_user, login_required
from flask_wtf.csrf import validate_csrf
from wtforms.validators import ValidationError

from . import assistant_bp
from .document_analysis import CorrespondenceIntakeError, analyze_uploaded_attachment
from .project_knowledge import index_stats, internal_knowledge_allowed, rebuild_project_index
from .service import answer, normalize_analysis_mode, summarize_content


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


def _analysis_text_limit() -> int:
    try:
        value = int(current_app.config.get("ASSISTANT_ANALYSIS_MAX_TEXT_CHARS", 60_000))
    except (TypeError, ValueError):
        value = 60_000
    return max(1_000, min(value, 200_000))


def _analysis_instruction(value) -> str:
    return str(value or "").strip()[:1_000]


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


@assistant_bp.route("/analyze", methods=["POST"])
@login_required
def analyze():
    """Summarize pasted text or one attachment using local-only processing."""
    try:
        validate_csrf(request.headers.get("X-CSRFToken", ""))
    except ValidationError:
        return jsonify({"error": "csrf", "message": "انتهت صلاحية الجلسة. حدّث الصفحة وحاول مجددًا."}), 400

    if not _rate_limit_allows(int(current_user.id)):
        return jsonify(
            {
                "error": "rate_limited",
                "message": "أرسلت طلبات كثيرة خلال وقت قصير. انتظر قليلًا ثم حاول مجددًا.",
            }
        ), 429

    attachment = None
    extracted: dict | None = None
    if request.is_json:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "invalid_json", "message": "تعذر قراءة المحتوى المراد تلخيصه."}), 400
        content = str(payload.get("text") or "").strip()
        instruction = _analysis_instruction(payload.get("instruction"))
        analysis_mode = normalize_analysis_mode(payload.get("analysis_mode"))
        source_label = "النص المرسل"
        if not content:
            return jsonify({"error": "empty_content", "message": "ألصق النص الذي تريد تلخيصه أولًا."}), 400
        if len(content) > _analysis_text_limit():
            return jsonify(
                {
                    "error": "text_too_long",
                    "message": f"اختصر النص إلى {_analysis_text_limit()} حرفًا أو أقل.",
                }
            ), 400
        analysis = {
            "kind": "text",
            "label": source_label,
            "character_count": len(content),
            "warnings": [],
        }
    elif request.mimetype == "multipart/form-data":
        attachment = request.files.get("file")
        instruction = _analysis_instruction(request.form.get("instruction"))
        analysis_mode = normalize_analysis_mode(request.form.get("analysis_mode"))
        if attachment is None or not str(getattr(attachment, "filename", "") or "").strip():
            return jsonify({"error": "missing_file", "message": "اختر مرفقًا لتحليله أولًا."}), 400
        try:
            extracted = analyze_uploaded_attachment(attachment)
        except CorrespondenceIntakeError as exc:
            return jsonify({"error": exc.code, "message": exc.message}), exc.status_code

        content = str(extracted.get("text") or "").strip()
        source_label = str(extracted.get("filename") or "المرفق")
        analysis = {
            "kind": "attachment",
            "label": source_label,
            "format": str(extracted.get("format") or "File"),
            "warnings": list(extracted.get("warnings") or [])[:8],
            "ocr": dict(extracted.get("ocr") or {}),
            "metadata": dict(extracted.get("metadata") or {}),
        }
    else:
        return jsonify(
            {
                "error": "invalid_content_type",
                "message": "أرسل نصًا بصيغة JSON أو مرفقًا بصيغة form-data.",
            }
        ), 415

    analysis["mode"] = analysis_mode
    try:
        result = summarize_content(
            current_user,
            content,
            instruction=instruction,
            source_label=source_label,
            analysis_mode=analysis_mode,
        )
        result["analysis"] = analysis
        current_app.logger.info(
            "Aref analyzed user_id=%s source=%s format=%s chars=%s warnings=%s",
            current_user.id,
            analysis["kind"],
            analysis.get("format", "text"),
            len(content),
            len(analysis.get("warnings") or []),
        )
    except Exception:
        current_app.logger.exception("Assistant analysis failed for user_id=%s", current_user.id)
        return jsonify(
            {
                "error": "assistant_analysis_failed",
                "message": "تعذر تحليل المحتوى الآن. حاول مرة أخرى بعد قليل.",
            }
        ), 500

    response = jsonify(result)
    response.headers["Cache-Control"] = "no-store"
    return response
