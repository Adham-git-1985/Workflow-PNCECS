"""Permission-aware help assistant for Masar and the administrative portal."""

from __future__ import annotations

import os
import re
import unicodedata
from typing import Any

from flask import current_app, url_for

from utils import system_search


_AR_DIACRITICS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
_SPACE = re.compile(r"\s+")

_SUGGESTIONS = (
    "كيف أنشئ طلبًا جديدًا؟",
    "أين أجد مهامي؟",
    "كيف أرفع ملفًا إلى الأرشيف؟",
    "أريد دليل الإجازات",
)

_NAVIGATION_INTENTS = (
    {
        "phrases": ("طلب جديد", "انشاء طلب", "إنشاء طلب", "انشئ طلب", "أنشئ طلب", "تقديم طلب"),
        "title": "إنشاء طلب جديد",
        "desc": "ابدأ معاملة جديدة واختر نوع الطلب والمسار المناسب.",
        "category": "مسار",
        "endpoint": "workflow.new_request",
    },
    {
        "phrases": ("مهامي", "صندوق الوارد", "المهام الواردة", "بانتظار موافقتي"),
        "title": "مهامي",
        "desc": "المعاملات التي تنتظر إجراءً منك.",
        "category": "مسار",
        "endpoint": "workflow.inbox",
    },
    {
        "phrases": ("طلباتي", "حالة طلبي", "متابعة طلبي", "الطلبات التي انشأتها"),
        "title": "طلباتي",
        "desc": "تابع الطلبات التي أنشأتها وحالتها الحالية.",
        "category": "مسار",
        "endpoint": "my_requests",
    },
    {
        "phrases": ("الاشعارات", "الإشعارات", "التنبيهات"),
        "title": "الإشعارات",
        "desc": "راجع إشعارات مسار والتنبيهات الجديدة.",
        "category": "مسار",
        "endpoint": "workflow.notifications",
    },
    {
        "phrases": ("الرسائل", "المراسلات", "صندوق الرسائل"),
        "title": "المراسلات الداخلية",
        "desc": "افتح صندوق المراسلات الداخلية.",
        "category": "مسار",
        "endpoint": "messages.inbox",
    },
    {
        "phrases": ("رفع ملف", "ارفع ملف", "الأرشيف", "الارشيف"),
        "title": "رفع ملف إلى الأرشيف",
        "desc": "ارفع ملفًا جديدًا وحدد بياناته وصلاحياته.",
        "category": "الأرشيف",
        "endpoint": "archive.upload_file",
    },
    {
        "phrases": ("ملفاتي", "الملفات الخاصة بي"),
        "title": "ملفاتي",
        "desc": "اعرض الملفات التي تملكها في الأرشيف.",
        "category": "الأرشيف",
        "endpoint": "archive.my_files",
    },
    {
        "phrases": ("طلب صلاحية", "طلب صلاحيه", "صلاحيات جديدة"),
        "title": "طلبات الصلاحيات",
        "desc": "قدّم طلب صلاحية أو تابع طلباتك السابقة.",
        "category": "البوابة الإدارية",
        "endpoint": "portal.my_access_requests",
    },
    {
        "phrases": ("ملفي الشخصي", "الملف الشخصي", "تغيير صورتي"),
        "title": "ملفي الشخصي",
        "desc": "راجع بيانات حسابك وصورتك الشخصية.",
        "category": "الحساب",
        "endpoint": "users.profile",
    },
    {
        "phrases": ("البوابة الإدارية", "البوابه الاداريه", "افتح البوابة"),
        "title": "البوابة الإدارية",
        "desc": "انتقل إلى أنظمة الموارد البشرية والمراسلات والمستودع والحركة.",
        "category": "البوابة الإدارية",
        "endpoint": "portal.portal_entry",
    },
    {
        "phrases": ("دليل الإجازات", "دليل الاجازات", "كيف اقدم اجازة", "طلب اجازة"),
        "title": "دليل الإجازات",
        "desc": "شرح تقديم طلبات الإجازة ومتابعتها واعتمادها.",
        "category": "الأدلة",
        "endpoint": "users.help_leaves_guide",
    },
    {
        "phrases": ("مركز الأدلة", "مركز الادلة", "المساعدة", "الدليل الشامل"),
        "title": "مركز الأدلة",
        "desc": "جميع أدلة النظام المتاحة حسب صلاحياتك.",
        "category": "الأدلة",
        "endpoint": "users.help_index",
    },
)

_SENSITIVE_ACTION_PHRASES = (
    "اعتمد بدلا مني",
    "وافق بدلا مني",
    "ارفض بدلا مني",
    "احذف المستخدم",
    "غير كلمة مرور",
    "اعطني كلمة مرور",
    "اظهر كلمة مرور",
    "تجاوز الصلاحيات",
    "ارفع صلاحيتي",
)


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = _AR_DIACRITICS.sub("", text).replace("ـ", "")
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    text = text.replace("ى", "ي").replace("ؤ", "و").replace("ئ", "ي").replace("ة", "ه")
    return _SPACE.sub(" ", text).casefold().strip()


def _compact(value: Any, limit: int) -> str:
    text = _SPACE.sub(" ", str(value or "")).strip()
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _is_page_question(message: str) -> bool:
    normalized = normalize_text(message)
    return any(
        phrase in normalized
        for phrase in ("هذه الصفحه", "هذي الصفحه", "الصفحه الحاليه", "اشرح الصفحه", "ماذا افعل هنا")
    )


def _direct_navigation_results(message: str) -> list[dict[str, str]]:
    normalized = normalize_text(message)
    results: list[dict[str, str]] = []
    for item in _NAVIGATION_INTENTS:
        phrases = tuple(normalize_text(phrase) for phrase in item["phrases"])
        if not any(phrase and phrase in normalized for phrase in phrases):
            continue
        try:
            href = url_for(str(item["endpoint"]))
        except Exception:
            continue
        results.append(
            {
                "id": f"assistant_{item['endpoint']}",
                "title": str(item["title"]),
                "desc": str(item["desc"]),
                "category": str(item["category"]),
                "href": href,
            }
        )
    return results


def navigation_results(user, message: str, context: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Return permission-aware destinations related to a user's question."""
    context = context or {}
    candidates = _direct_navigation_results(message)

    try:
        candidates.extend(system_search.search(user, message, limit=6))
    except Exception:
        current_app.logger.exception("Assistant system search failed")

    if _is_page_question(message):
        page_title = _compact(context.get("title"), 120)
        if page_title:
            try:
                candidates.extend(system_search.search(user, page_title, limit=4))
            except Exception:
                current_app.logger.exception("Assistant page-context search failed")

    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in candidates:
        href = str(item.get("href") or "").strip()
        if not href or href == "#" or not href.startswith("/") or href in seen:
            continue
        seen.add(href)
        results.append(
            {
                "title": _compact(item.get("title"), 100),
                "desc": _compact(item.get("desc"), 180),
                "category": _compact(item.get("category"), 80),
                "href": href,
            }
        )
        if len(results) >= 5:
            break
    return results


def build_local_reply(
    message: str,
    results: list[dict[str, str]] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a useful Arabic answer without sending data to an external service."""
    results = results or []
    context = context or {}
    normalized = normalize_text(message)

    if any(normalize_text(phrase) in normalized for phrase in _SENSITIVE_ACTION_PHRASES):
        reply = (
            "حفاظًا على أمان حسابك والصلاحيات، لا أستطيع تنفيذ الموافقات أو الحذف "
            "أو كشف كلمات المرور نيابةً عنك. أستطيع إرشادك إلى الشاشة الصحيحة لتنفّذ الإجراء بنفسك."
        )
    elif any(word in normalized for word in ("مرحبا", "اهلا", "السلام عليكم", "صباح الخير", "مساء الخير")):
        reply = (
            "أهلًا بك! أنا مساعد مسار. أساعدك في الوصول إلى الشاشات والأدلة وفهم خطوات العمل. "
            "اكتب ما تريد إنجازه، مثل: كيف أنشئ طلبًا جديدًا؟"
        )
    elif _is_page_question(message):
        title = _compact(context.get("title"), 100) or "الصفحة الحالية"
        if results:
            reply = f"أنت الآن في «{title}». وجدت أدلة أو شاشات مرتبطة قد تساعدك في فهم ما يمكنك فعله هنا."
        else:
            reply = (
                f"أنت الآن في «{title}». لم أجد شرحًا مطابقًا مباشرةً، لكن يمكنك وصف الزر أو المهمة "
                "التي تريد تنفيذها وسأحاول توجيهك."
            )
    elif results:
        first = results[0]
        reply = f"أنسب نتيجة لسؤالك هي «{first['title']}». {first['desc']}"
        if len(results) > 1:
            reply += " أضفت أيضًا خيارات مرتبطة قد تكون مفيدة."
    else:
        reply = (
            "لم أجد شاشة مطابقة بوضوح. جرّب ذكر اسم القسم أو المهمة، مثل: طلب جديد، مهامي، "
            "الإجازات، الأرشيف، الموارد البشرية، المراسلات أو الصلاحيات."
        )

    return {
        "reply": reply,
        "mode": "local",
        "links": results,
        "suggestions": list(_SUGGESTIONS),
    }


def _try_external_ai(
    message: str,
    history: list[dict[str, str]],
    context: dict[str, Any],
    results: list[dict[str, str]],
) -> str | None:
    enabled = str(
        current_app.config.get("ASSISTANT_AI_ENABLED")
        or os.getenv("ASSISTANT_AI_ENABLED", "")
    ).strip().lower() in {"1", "true", "yes", "on"}
    api_key = current_app.config.get("ASSISTANT_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    model = current_app.config.get("ASSISTANT_OPENAI_MODEL") or os.getenv("OPENAI_CHAT_MODEL")
    if not enabled or not api_key or not model:
        return None

    try:
        from openai import OpenAI

        allowed_destinations = "\n".join(
            f"- {item['title']}: {item['desc']} ({item['href']})"
            for item in results
        ) or "- لا توجد وجهات مطابقة مؤكدة."
        page_title = _compact(context.get("title"), 120) or "غير محددة"
        page_path = _compact(context.get("path"), 180) or "/"
        instructions = (
            "أنت مساعد إرشادي عربي داخل نظام مسار والبوابة الإدارية. "
            "أجب باختصار ووضوح اعتمادًا فقط على وجهات النظام المسموح بها أدناه. "
            "لا تدّعِ تنفيذ أي إجراء، ولا تطلب كلمات مرور أو رموزًا أو بيانات شخصية، "
            "ولا تخمّن صلاحيات غير ظاهرة. عند الشك وجّه المستخدم إلى مركز الأدلة.\n\n"
            f"الصفحة الحالية: {page_title} ({page_path})\n"
            f"الوجهات المسموح بها:\n{allowed_destinations}"
        )
        input_messages: list[dict[str, str]] = []
        for item in history[-6:]:
            role = item.get("role")
            content = _compact(item.get("content"), 800)
            if role in {"user", "assistant"} and content:
                input_messages.append({"role": role, "content": content})
        input_messages.append({"role": "user", "content": _compact(message, 1200)})

        client = OpenAI(
            api_key=api_key,
            timeout=float(current_app.config.get("ASSISTANT_AI_TIMEOUT", 20)),
            max_retries=1,
        )
        response = client.responses.create(
            model=str(model),
            instructions=instructions,
            input=input_messages,
            max_output_tokens=450,
        )
        reply = _compact(getattr(response, "output_text", ""), 2400)
        return reply or None
    except Exception:
        current_app.logger.exception("External assistant failed; using local fallback")
        return None


def answer(
    user,
    message: str,
    history: list[dict[str, str]] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    history = history or []
    context = context or {}
    results = navigation_results(user, message, context)
    local = build_local_reply(message, results, context)
    ai_reply = _try_external_ai(message, history, context, results)
    if ai_reply:
        local["reply"] = ai_reply
        local["mode"] = "ai"
    return local
