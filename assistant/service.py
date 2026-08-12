"""Permission-aware service for ``اسأل عارف``."""

from __future__ import annotations

import os
import re
import unicodedata
import hashlib
from functools import lru_cache
from typing import Any

from flask import current_app, url_for

from .knowledge import collect_knowledge
from utils import system_search


_AR_DIACRITICS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
_SPACE = re.compile(r"\s+")

_SUGGESTIONS = (
    "اشرح هذه الصفحة",
    "لا أعرف من أين أبدأ",
    "ما هي صلاحياتي؟",
    "ما الطلبات التي تخصني؟",
)

_ADMIN_SUGGESTIONS = (
    "اشرح هيكلية المشروع",
    "ما جداول قاعدة البيانات؟",
    "كيف يعمل نظام الصلاحيات في الكود؟",
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
    "كلمة مرور",
    "كلمات المرور",
    "بيانات الدخول",
    "مفتاح api",
    "api key",
    "secret key",
    "تجاوز الصلاحيات",
    "ارفع صلاحيتي",
)

# External AI is deliberately limited to public, general conversation.  These
# terms describe internal government work, personal records, project internals,
# or credentials and therefore force the request to remain on the local path.
_EXTERNAL_AI_BLOCKED_PHRASES = (
    "الطلب", "طلب رقم", "المعامله", "المعاملة", "المعاملات", "المسار", "المسارات",
    "المهمه", "المهمة", "المهام", "الموافقه", "الموافقة", "الاعتماد", "الصلاحيه", "الصلاحية",
    "المراسله", "المراسلة", "المراسلات", "الوارد", "الصادر", "كتاب رسمي", "خطاب رسمي",
    "الاجتماع", "محضر", "اللجنه", "اللجنة", "القرار", "القرارات", "التعميم",
    "الموظف", "الموظفين", "المستخدم", "الحساب", "الراتب", "الرواتب", "قسيمه", "قسيمة",
    "الهويه", "الهوية", "رقم وطني", "رقم الهويه", "رقم الهوية", "الجوال", "الهاتف",
    "العنوان", "البريد", "الاجازه", "الإجازة", "الاجازات", "الإجازات", "الدوام", "الحضور",
    "الغياب", "التقييم", "القضيه", "القضية", "التحقيق", "العقوبه", "العقوبة",
    "الملف", "المرفق", "الارشيف", "الأرشيف", "سري", "سريه", "سرية", "حكومي", "حكوميه",
    "قاعده البيانات", "قاعدة البيانات", "الجدول", "الجداول", "الكود", "المشروع", "السيرفر",
    "كلمه المرور", "كلمة المرور", "رمز الدخول", "مفتاح api", "api key", "secret", "password",
    "اشرح هذه الصفحه", "اشرح هذه الصفحة", "الشاشه الحاليه", "الشاشة الحالية",
)

_EXTERNAL_AI_STRUCTURED_SECRET = re.compile(
    r"(?i)(?:bearer\s+[a-z0-9._-]+|(?:api[_-]?key|secret|password|token)\s*[:=])"
)
_EXTERNAL_AI_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_EXTERNAL_AI_URL_OR_PATH = re.compile(
    r"(?i)(?:https?://|file://|[A-Z]:[\\/]|(?:^|\s)/(?:home|var|etc|srv|opt|mnt)/)"
)
_EXTERNAL_AI_NUMBER_IDENTIFIER = re.compile(r"[0-9٠-٩]{4,}")

_CONTEXTUAL_FOLLOW_UP_EXACT = {
    "وضح اكثر", "اشرح اكثر", "ماذا تقصد", "مش واضح", "غير واضح",
    "كمل", "اكمل", "تابع", "ثم ماذا", "وماذا بعد", "ما الحل",
    "اعطني مثالا", "اعطيني مثالا", "اختصر", "اختصرها", "اختصره",
    "لخص", "لخصها", "لخصه", "لماذا", "كيف ذلك",
}

_CONTEXTUAL_FOLLOW_UP_MARKERS = (
    "هذا الموضوع", "ذلك الموضوع", "الموضوع السابق", "نفس الموضوع",
    "ردك السابق", "كلامك السابق", "ما ذكرته", "الذي ذكرته",
    "ما قلته", "الذي قلته", "الرد السابق",
)

_HOW_TO_GUIDES = (
    {
        "phrases": ("كيف انشئ طلب", "كيف اقدم طلب", "خطوات انشاء طلب", "طلب جديد"),
        "reply": (
            "لإنشاء طلب جديد:\n"
            "1) افتح «مسار» ثم «طلب جديد».\n"
            "2) اختر نوع الطلب والمسار المناسب من المسارات المنشأة في النظام.\n"
            "3) اكتب العنوان والوصف وأرفق الملفات المطلوبة.\n"
            "4) راجع البيانات ثم أنشئ الطلب؛ بعدها تابعه من «طلباتي».\n"
            "إذا لم يظهر لك نوع طلب أو مسار، فهذا يعني أنه غير متاح لصلاحيتك أو لم تتم تهيئته بعد."
        ),
    },
    {
        "phrases": ("كيف اعالج مهمه", "كيف اعتمد طلب", "كيف اوافق على طلب", "اين اجد مهامي"),
        "reply": (
            "لمعالجة مهمة:\n"
            "1) افتح «مهامي / صندوق الوارد».\n"
            "2) افتح المعاملة واقرأ التفاصيل والمرفقات وسجل الإجراءات.\n"
            "3) إذا كانت الخطوة مسندة إليك ستظهر أزرار الإجراء المسموح بها.\n"
            "4) اكتب الملاحظة ثم نفّذ القرار بنفسك. عارف لا يعتمد أو يرفض نيابةً عنك."
        ),
    },
    {
        "phrases": ("كيف اطلب صلاحيه", "طلب صلاحيه", "صلاحيات جديده"),
        "reply": (
            "لطلب صلاحية:\n"
            "1) افتح «طلبات الصلاحيات».\n"
            "2) اختر الصلاحية المطلوبة واكتب سبب الحاجة إليها.\n"
            "3) أرسل الطلب وتابع حالته من الصفحة نفسها.\n"
            "تفعيل الصلاحية يتم بعد اعتماد الجهة المخولة وفق التسلسل الإداري."
        ),
    },
    {
        "phrases": ("كيف اسجل وارد", "كيف اسجل صادر", "كيف ابدا مسار للمراسله", "خطوات المراسلات"),
        "reply": (
            "لتسجيل مراسلة وتشغيل مسارها:\n"
            "1) افتح البوابة الإدارية ثم «الصادر والوارد» إذا كانت صلاحيتك تسمح.\n"
            "2) اختر واردًا أو صادرًا وأدخل المرجع والجهة والموضوع والتاريخ.\n"
            "3) حدد الأولوية ودرجة السرية؛ عند اختيار «سري» حدد المستخدمين المخولين.\n"
            "4) اختر مسارًا من قوالب المسارات المنشأة في نظام مسار ثم ابدأ المعاملة.\n"
            "تبقى السرية وحجب المستخدمين قائمين بعد انتقال المراسلة إلى المسار والأرشيف."
        ),
    },
    {
        "phrases": ("كيف ارفع ملف", "رفع ملف للارشيف", "خطوات الارشيف"),
        "reply": (
            "لرفع ملف إلى الأرشيف:\n"
            "1) افتح «الأرشيف» ثم «رفع ملف».\n"
            "2) اختر الملف وأدخل عنوانه ووصفه وتصنيفه.\n"
            "3) حدد مستوى الوصول والمشاركة ثم احفظ.\n"
            "لن يظهر الملف إلا لمن تسمح له ملكية الملف وصلاحيات الأرشيف والسرية المرتبطة به."
        ),
    },
    {
        "phrases": ("كيف اقدم اجازه", "طلب اجازه", "خطوات الاجازه"),
        "reply": (
            "لتقديم طلب إجازة:\n"
            "1) افتح البوابة الإدارية ثم الخدمة الذاتية للموظف.\n"
            "2) اختر الإجازات ثم أنشئ طلبًا وحدد النوع والفترة.\n"
            "3) أرفق المستندات المطلوبة وأرسل الطلب.\n"
            "4) تابع الموافقات والحالة من طلباتك وإشعاراتك."
        ),
    },
)

_HELP_OVERVIEW_PHRASES = (
    "لا اعرف من اين ابدا",
    "ماذا يستطيع عارف",
    "كيف تساعدني",
    "انواع المساعده",
    "احتاج مساعده",
)

_HELP_OVERVIEW_REPLY = (
    "لا تحتاج إلى معرفة صياغة السؤال. من زر القائمة أعلى نافذة عارف اختر نوع المساعدة:\n"
    "1) «اشرح هذه الصفحة» لفهم الشاشة الحالية وما يمكنك فعله فيها.\n"
    "2) «أنجز مهمة» للحصول على خطوات الطلبات والإجازات والمراسلات والأرشيف والصلاحيات.\n"
    "3) «خذني إلى شاشة» للوصول مباشرةً إلى المكان الصحيح.\n"
    "4) «ابحث واعرض بياناتي» لطلباتك وإشعاراتك ومراسلاتك ودليل الموظفين ضمن صلاحياتك.\n"
    "5) «حل مشكلة» للصفحات والأزرار والطلبات المتوقفة والمرفقات والجلسة.\n"
    "6) «الأدلة الكاملة» لفتح أدلة النظام.\n"
    "ويمكنك دائمًا كتابة ما تريد بطريقتك، حتى لو كان وصفًا قصيرًا مثل «لا أجد زر الاعتماد»."
)

_TROUBLESHOOTING_GUIDES = (
    {
        "phrases": ("الصفحه لا تفتح", "لا استطيع فتح الصفحه", "غير مصرح", "منع الوصول", "رفض الوصول"),
        "reply": (
            "لحل مشكلة فتح الصفحة:\n"
            "1) حدّث الصفحة مرة واحدة وتأكد أن جلسة الدخول ما زالت فعّالة.\n"
            "2) افتح الصفحة من قائمة النظام بدل رابط قديم أو محفوظ.\n"
            "3) إذا ظهرت «غير مصرح»، فالشاشة تحتاج دورًا أو صلاحية غير موجودة في حسابك؛ راجع «صلاحياتي» أو قدّم طلب صلاحية.\n"
            "4) إذا بقيت المشكلة، اذكر لي اسم الشاشة ونص رسالة الخطأ كما ظهر وسأحدد لك الخطوة التالية.\n"
            "لا ترسل كلمة المرور أو رمز الدخول."
        ),
    },
    {
        "phrases": ("الزر لا يظهر", "لا يظهر لي الزر", "لا اجد الزر", "الخيار لا يظهر", "الخيار غير ظاهر", "زر مفقود"),
        "reply": (
            "اختفاء زر أو خيار يكون غالبًا بسبب واحد من هذه الأمور:\n"
            "1) الإجراء غير مسموح لصلاحية حسابك.\n"
            "2) السجل ليس في الحالة التي تسمح بهذا الإجراء، مثل طلب مكتمل أو مرفوض.\n"
            "3) المهمة ليست مسندة إليك أو لم تصل إلى خطوتك بعد.\n"
            "4) بعض الحقول المطلوبة لم تُستكمل بعد.\n"
            "افتح السجل وتحقق من حالته والخطوة الحالية، ثم أخبرني باسم الشاشة واسم الزر المفقود ورقم الطلب إن وجد."
        ),
    },
    {
        "phrases": ("الطلب متوقف", "الطلب عالق", "لا يتحرك الطلب", "لا ينتقل الطلب", "لم ينتقل الطلب", "الخطوه التاليه"),
        "reply": (
            "للتحقق من طلب متوقف:\n"
            "1) افتح الطلب من «طلباتي» واقرأ الحالة وسجل الإجراءات.\n"
            "2) تحقق من الخطوة الحالية ومن الشخص أو الجهة المسندة إليها.\n"
            "3) تأكد أن الإجراء السابق حُفظ فعلًا وأن الحقول أو المرفقات المطلوبة مكتملة.\n"
            "4) راجع الإشعارات لمعرفة إن كان الطلب أُعيد إليك أو رُفض.\n"
            "إذا لم يظهر مسؤول للخطوة أو بقيت دون حركة، أرسل رقم الطلب لمسؤول المسارات ليراجع القالب والتوجيه. يمكنك أيضًا كتابة «حالة الطلب رقم 123» ليعرض عارف ما تسمح لك صلاحيتك برؤيته."
        ),
    },
    {
        "phrases": ("فشل رفع الملف", "لا استطيع رفع ملف", "المرفق لا يرفع", "مشكله في المرفق", "فشل المرفق", "لا يفتح المرفق"),
        "reply": (
            "لحل مشكلة الملف أو المرفق:\n"
            "1) تأكد أن نوع الملف وحجمه مسموحان في الشاشة.\n"
            "2) أعد تسمية الملف باسم قصير وواضح دون رموز غريبة، ثم جرّب مجددًا.\n"
            "3) انتظر اكتمال الرفع قبل الحفظ، وتحقق من ثبات الاتصال.\n"
            "4) حدّث الصفحة وأعد اختيار الملف إذا انتهت الجلسة.\n"
            "إذا استمرت المشكلة، أخبرني باسم الشاشة وامتداد الملف وحجمه ونص الخطأ، من دون إرسال محتوى حساس."
        ),
    },
    {
        "phrases": ("القائمه فارغه", "البيانات لا تظهر", "نتيجه مفقوده", "لا تظهر النتائج", "لا اجد السجل"),
        "reply": (
            "إذا كانت القائمة فارغة أو السجل غير ظاهر:\n"
            "1) امسح مرشحات البحث ووسّع نطاق التاريخ ثم أعد البحث.\n"
            "2) تأكد أنك في القسم الصحيح وأن حالة السجل ليست مخفية بفلتر.\n"
            "3) قد يكون السجل خارج نطاق صلاحيتك أو دائرتك، أو مصنفًا سريًا.\n"
            "4) ابحث بالرقم الدقيق إن كان متاحًا.\n"
            "اكتب اسم نوع السجل ورقمه أو موضوعه، وسيبحث عارف في البيانات المسموح لك بها."
        ),
    },
    {
        "phrases": ("انتهت الجلسه", "تم تسجيل خروجي", "انتهت صلاحيه الجلسه", "تعذر الحفظ بعد تسجيل الدخول"),
        "reply": (
            "عند انتهاء الجلسة:\n"
            "1) انسخ أي نص لم تحفظه إن كان ما زال ظاهرًا.\n"
            "2) حدّث الصفحة وسجّل الدخول من جديد.\n"
            "3) ارجع إلى السجل وتأكد هل حُفظ الإجراء قبل إعادة إرساله حتى لا يتكرر.\n"
            "إذا تكرر الخروج مباشرةً، تواصل مع مسؤول النظام واذكر وقت المشكلة واسم الصفحة فقط؛ لا تشارك كلمة المرور."
        ),
    },
    {
        "phrases": ("حدث خطا", "رساله خطا", "مشكله اخرى", "ساعدني في مشكله"),
        "reply": (
            "سأساعدك في تشخيصها. اكتب في رسالة واحدة:\n"
            "1) اسم الشاشة.\n"
            "2) ما الذي حاولت فعله.\n"
            "3) نص رسالة الخطأ كما ظهر.\n"
            "4) رقم الطلب أو السجل إن كان مسموحًا لك عرضه.\n"
            "لا ترسل كلمة مرور أو رمز دخول أو بيانات سرية غير لازمة."
        ),
    },
)

_CASUAL_EXACT_RESPONSES = {
    "شكرا": "على الرحب والسعة. أنا معك؛ أكمل كلامك أو أخبرني بما تريد فعله.",
    "شكرا لك": "العفو، هذا دوري. هل نكمل في نفس الموضوع أم تريد مساعدة في شيء آخر؟",
    "مشكور": "العفو. أنا حاضر إذا أردت أن نكمل.",
    "تمام": "تمام، أنا معك. أكمل أو أخبرني بالخطوة التالية التي تريدها.",
    "حسنا": "حسنًا. أكمل بطريقتك، وسأتابع معك.",
    "اوكي": "تمام. ماذا تريد أن نفعل بعد ذلك؟",
    "نعم": "حسنًا، نكمل. اكتب لي ما تريد أو اختر نوع المساعدة من زر القائمة.",
    "لا": "لا مشكلة. قل لي بطريقتك ما الذي لم يناسبك أو ماذا تريد بدلًا منه.",
    "مع السلامه": "مع السلامة. سأكون هنا عندما تحتاج إلى مساعدة.",
    "الى اللقاء": "إلى اللقاء. يمكنك العودة وإكمال الحديث في أي وقت داخل هذه الجلسة.",
}

_CASUAL_GREETING_PHRASES = (
    "مرحبا", "اهلا", "السلام عليكم", "صباح الخير", "مساء الخير", "هلا", "هاي",
)

_CASUAL_DIFFICULTY_PHRASES = (
    "الموضوع صعب", "الامر صعب", "مش فاهم", "لا افهم", "ضايع", "محتار", "متضايق",
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


def _how_to_reply(message: str) -> str | None:
    normalized = normalize_text(message)
    for guide in _HOW_TO_GUIDES:
        if any(normalize_text(phrase) in normalized for phrase in guide["phrases"]):
            return str(guide["reply"])
    return None


def _guided_help_reply(message: str) -> str | None:
    normalized = normalize_text(message)
    if any(normalize_text(phrase) in normalized for phrase in _HELP_OVERVIEW_PHRASES):
        return _HELP_OVERVIEW_REPLY
    for guide in _TROUBLESHOOTING_GUIDES:
        if any(normalize_text(phrase) in normalized for phrase in guide["phrases"]):
            return str(guide["reply"])
    return None


def _last_history_message(history: list[dict[str, str]], role: str) -> str:
    for item in reversed(history or []):
        if item.get("role") == role and str(item.get("content") or "").strip():
            return str(item["content"]).strip()
    return ""


def _casual_conversation_reply(message: str, history: list[dict[str, str]] | None = None) -> str | None:
    """Handle common conversational Arabic when the external model is unavailable."""
    normalized = normalize_text(message)
    history = history or []
    exact = _CASUAL_EXACT_RESPONSES.get(normalized)
    if exact:
        return exact
    if any(normalize_text(phrase) in normalized for phrase in ("كيف حالك", "شو اخبارك", "كيفك")):
        return "أنا بخير وجاهز أساعدك. كيف حالك أنت، وما الذي تحب أن نتحدث عنه أو ننجزه؟"
    if any(normalize_text(phrase) in normalized for phrase in ("من انت", "عرفني عن نفسك", "ما اسمك")):
        return (
            "أنا عارف، مساعدك داخل مسار والبوابة الإدارية. أستطيع التحدث معك، شرح الشاشات، "
            "إرشادك خطوة بخطوة، والبحث في المعلومات التي تسمح بها صلاحيات حسابك."
        )
    if any(normalize_text(phrase) in normalized for phrase in _CASUAL_DIFFICULTY_PHRASES):
        return (
            "أفهمك، وخلّينا نبسّطها معًا. لا تحتاج أن تعرف اسم الشاشة أو المصطلح الصحيح؛ "
            "قل لي فقط ما النتيجة التي تريد الوصول إليها، وسأمشي معك خطوة خطوة."
        )
    if any(normalize_text(phrase) in normalized for phrase in ("وضح اكثر", "اشرح اكثر", "ماذا تقصد", "مش واضح")):
        previous = _last_history_message(history, "assistant")
        if previous:
            return (
                "بكل تأكيد. أخبرني أي جزء من ردي السابق تريد تبسيطه، أو اذكر الخطوة التي توقفت عندها، "
                "وسأشرحها بطريقة أقصر وبمثال مباشر."
            )
    if any(normalize_text(phrase) in normalized for phrase in _CASUAL_GREETING_PHRASES):
        return (
            "أهلًا وسهلًا! أنا عارف، وأنا معك. يمكنك أن تكلمني بطريقتك العادية؛ "
            "هل تريد إنجاز مهمة، فهم شاشة، البحث عن معلومة، أم فقط تسألني سؤالًا؟"
        )
    if any(normalize_text(phrase) in normalized for phrase in ("نتكلم", "نتحدث", "سولف معي", "كلمني")):
        return "أكيد، نتكلم بشكل عادي. ابدأ بأي موضوع أو أخبرني بما يشغلك، وسأتابع معك ضمن إمكانات وضع المحادثة المتاح."
    return None


def _privacy_safe_user_identifier(user: Any) -> str:
    secret = str(current_app.config.get("SECRET_KEY") or "aref-assistant")
    user_id = str(getattr(user, "id", "anonymous"))
    digest = hashlib.sha256(f"{secret}:{user_id}".encode("utf-8")).hexdigest()[:32]
    return f"aref_{digest}"


def _external_ai_privacy_mode() -> str:
    mode = str(current_app.config.get("ASSISTANT_AI_PRIVACY_MODE") or "LOCAL_ONLY").strip().upper()
    return mode if mode in {"LOCAL_ONLY", "PUBLIC_ONLY"} else "LOCAL_ONLY"


def _knowledge_contains_internal_data(knowledge: dict[str, Any]) -> bool:
    return any(
        knowledge.get(key)
        for key in ("reply", "facts", "sources", "evidence", "intents", "index_stats")
    )


def _public_message_allowed(value: Any) -> tuple[bool, str]:
    text = str(value or "").strip()
    if not text:
        return False, "empty"
    max_chars = max(80, int(current_app.config.get("ASSISTANT_AI_PUBLIC_MAX_CHARS", 600)))
    if len(text) > max_chars or "\n" in text or "\r" in text:
        return False, "long_or_pasted"
    normalized = normalize_text(text)
    if any(normalize_text(phrase) in normalized for phrase in _EXTERNAL_AI_BLOCKED_PHRASES):
        return False, "government_or_system_topic"
    if _EXTERNAL_AI_STRUCTURED_SECRET.search(text):
        return False, "credential_pattern"
    if _EXTERNAL_AI_EMAIL.search(text):
        return False, "email_pattern"
    if _EXTERNAL_AI_URL_OR_PATH.search(text):
        return False, "url_or_path_pattern"
    if _EXTERNAL_AI_NUMBER_IDENTIFIER.search(text):
        return False, "identifier_pattern"
    return True, "public_conversation"


def _message_depends_on_history(value: Any) -> bool:
    """Return true for short follow-ups that have no safe standalone context."""

    normalized = normalize_text(str(value or ""))
    if normalized in _CONTEXTUAL_FOLLOW_UP_EXACT:
        return True
    return len(normalized.split()) <= 12 and any(
        normalize_text(marker) in normalized
        for marker in _CONTEXTUAL_FOLLOW_UP_MARKERS
    )


def _external_ai_privacy_decision(
    message: str,
    history: list[dict[str, str]],
    knowledge: dict[str, Any],
) -> tuple[bool, str]:
    """Fail closed unless the complete outbound conversation is public-only."""

    mode = _external_ai_privacy_mode()
    if mode == "LOCAL_ONLY":
        return False, "local_only"
    if _knowledge_contains_internal_data(knowledge):
        return False, "internal_knowledge"

    allowed, reason = _public_message_allowed(message)
    if not allowed:
        return False, reason
    # No history is ever transmitted. A self-contained public question starts
    # a fresh external turn, so protected older turns cannot permanently trap
    # the session in local mode. Ambiguous follow-ups remain local because they
    # depend on prior context that is intentionally kept on this server.
    if _message_depends_on_history(message):
        return False, "contextual_follow_up"
    return True, "public_conversation"


@lru_cache(maxsize=1)
def _windows_trust_context():
    """Build a verified TLS context from the Windows certificate stores.

    Some embedded Windows Python distributions cannot let OpenSSL read its
    default CA file and terminate with ``OPENSSL_Applink``. Loading the same
    trusted Windows certificates from memory avoids that runtime mismatch
    without weakening certificate or hostname verification.
    """
    if os.name != "nt":
        return None

    import ssl

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    loaded = 0
    for store_name in ("ROOT", "CA"):
        for certificate, encoding, _trust in ssl.enum_certificates(store_name):
            if encoding != "x509_asn":
                continue
            try:
                context.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(certificate))
                loaded += 1
            except ssl.SSLError:
                continue
    if not loaded:
        raise RuntimeError("تعذر تحميل شهادات Windows الموثوقة لاتصال عارف.")
    return context


def _openai_http_client(timeout: float):
    if os.name != "nt":
        return None
    import httpx

    return httpx.Client(verify=_windows_trust_context(), timeout=timeout)


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
    knowledge: dict[str, Any] | None = None,
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build an Arabic answer without sending data to an external service."""
    results = results or []
    context = context or {}
    knowledge = knowledge or {}
    history = history or []
    normalized = normalize_text(message)
    knowledge_reply = str(knowledge.get("reply") or "").strip()
    how_to_reply = _how_to_reply(message)
    guided_help_reply = _guided_help_reply(message)
    casual_reply = _casual_conversation_reply(message, history)
    if len(knowledge_reply) > 7200:
        knowledge_reply = knowledge_reply[:7199].rstrip() + "…"

    if any(normalize_text(phrase) in normalized for phrase in _SENSITIVE_ACTION_PHRASES):
        reply = (
            "حفاظًا على أمان حسابك والصلاحيات، لا أستطيع تنفيذ الموافقات أو الحذف "
            "أو كشف كلمات المرور نيابةً عنك. أستطيع إرشادك إلى الشاشة الصحيحة لتنفّذ الإجراء بنفسك."
        )
    elif knowledge_reply:
        reply = knowledge_reply
    elif how_to_reply:
        reply = how_to_reply
    elif guided_help_reply:
        reply = guided_help_reply
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
    elif casual_reply:
        reply = casual_reply
    else:
        reply = (
            "وصلتني رسالتك، وأنا معك. في وضع المحادثة المحلي أستطيع التفاعل اليومي البسيط "
            "ومساعدتك داخل النظام، أما الحوار العام المفتوح فيحتاج تفعيل وضع الذكاء الاصطناعي. "
            "يمكنك الآن توضيح مقصدك بكلمات أخرى، أو اختيار نوع المساعدة من زر القائمة أعلى النافذة."
        )

    suggestion_items = list(_SUGGESTIONS)
    if knowledge.get("access_level") in {"admin", "super_admin"}:
        suggestion_items.extend(_ADMIN_SUGGESTIONS)

    return {
        "reply": reply,
        "mode": "local",
        "links": results,
        "sources": list(knowledge.get("sources") or []),
        "suggestions": suggestion_items,
        "access_level": knowledge.get("access_level", "employee"),
        "access_label": knowledge.get("access_label", "نطاق المستخدم وصلاحياته"),
        "index_stats": knowledge.get("index_stats"),
    }


def _merge_links(*groups: list[dict[str, str]]) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    seen: set[str] = set()
    for group in groups:
        for item in group or []:
            href = str(item.get("href") or "").strip()
            if not href or href in seen:
                continue
            seen.add(href)
            merged.append(item)
            if len(merged) >= 6:
                return merged
    return merged


def _try_external_ai(
    user,
    message: str,
    history: list[dict[str, str]],
    context: dict[str, Any],
    results: list[dict[str, str]],
    knowledge: dict[str, Any],
) -> str | None:
    enabled = str(
        current_app.config.get("ASSISTANT_AI_ENABLED")
        or os.getenv("ASSISTANT_AI_ENABLED", "")
    ).strip().lower() in {"1", "true", "yes", "on"}
    api_key = current_app.config.get("ASSISTANT_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    model = current_app.config.get("ASSISTANT_OPENAI_MODEL") or os.getenv("OPENAI_CHAT_MODEL")
    if not enabled or not api_key or not model:
        return None

    external_allowed, privacy_reason = _external_ai_privacy_decision(message, history, knowledge)
    if not external_allowed:
        current_app.logger.info(
            "External assistant blocked by privacy gate reason=%s",
            privacy_reason,
        )
        return None

    try:
        from openai import OpenAI

        instructions = (
            "أنت «عارف»، مساعد عربي ودود للمحادثة العامة فقط. "
            "لم تُرسل إليك أي بيانات من النظام الحكومي أو الصفحة الحالية أو ملفات المشروع. "
            "لا تطلب من المستخدم أسماء أشخاص أو أرقام هويات أو مراسلات أو رواتب أو ملفات أو كلمات مرور. "
            "إذا تحول الحوار إلى عمل حكومي أو بيانات شخصية أو داخلية، اطلب منه استخدام المساعدة المحلية داخل النظام "
            "من دون كتابة المحتوى الحساس في المحادثة الخارجية. لا تدّعِ الوصول إلى النظام أو تنفيذ أي إجراء."
        )
        # History is inspected by the privacy gate but never transmitted.  A
        # clean current message is the only user content allowed off-server.
        input_messages: list[dict[str, str]] = [{
            "role": "user",
            "content": _compact(message, int(current_app.config.get("ASSISTANT_AI_PUBLIC_MAX_CHARS", 600))),
        }]

        timeout = float(current_app.config.get("ASSISTANT_AI_TIMEOUT", 20))
        client_options: dict[str, Any] = {
            "api_key": api_key,
            "timeout": timeout,
            "max_retries": 1,
        }
        http_client = _openai_http_client(timeout)
        if http_client is not None:
            client_options["http_client"] = http_client
        client = OpenAI(
            **client_options,
        )
        try:
            response = client.responses.create(
                model=str(model),
                instructions=instructions,
                input=input_messages,
                max_output_tokens=int(current_app.config.get("ASSISTANT_AI_MAX_OUTPUT_TOKENS", 1100)),
                safety_identifier=_privacy_safe_user_identifier(user),
                store=False,
            )
        finally:
            client.close()
        reply = _compact(getattr(response, "output_text", ""), 6000)
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
    knowledge = collect_knowledge(user, message, context)
    navigation = navigation_results(user, message, context)
    results = _merge_links(knowledge.get("links") or [], navigation)
    local = build_local_reply(message, results, context, knowledge, history)
    ai_reply = _try_external_ai(user, message, history, context, results, knowledge)
    if ai_reply:
        local["reply"] = ai_reply
        local["mode"] = "ai"
    local["intents"] = knowledge.get("intents") or []
    return local
