import re


UI_LABELS_AR = {
    "ADMIN_UPDATED_USER_PROFILE_FIELDS": "قام المسؤول بتحديث حقول ملف المستخدم",
    "ADMIN_UPDATED_مستخدم_PROFILE_FIELDS": "قام المسؤول بتحديث حقول ملف المستخدم",
    "ACTIVE": "نشط",
    "ARCHIVE": "الأرشيف",
    "ARCHIVED_FILE": "ملف مؤرشف",
    "ATTENDANCE_IMPORT": "استيراد الدوام",
    "CORR_ARCHIVE_LINK": "ربط المراسلة بالأرشيف",
    "CORR_ATTACH_UPLOAD": "رفع مرفق مراسلة",
    "CORR_INBOUND": "وارد",
    "CORR_IN_CREATE": "إنشاء مراسلة واردة",
    "CORR_OUT_CREATE": "إنشاء مراسلة صادرة",
    "CORR_OUTBOUND": "صادر",
    "CORR_WORKFLOW_START": "بدء مسار مراسلة",
    "CORR_OPEN": "فتح وبدء إجراء المراسلة",
    "CORR_REPLY": "الرد على المراسلة",
    "CORR_FORWARD": "تحويل المراسلة",
    "CORR_RETURN": "إعادة المراسلة إلى مرسلها",
    "CORR_INTERNAL_NOTE": "إضافة ملاحظة داخلية للمراسلة",
    "CORR_REQUEST_INFO": "طلب استكمال معلومات أو مرفقات",
    "CORR_SAVE_DRAFT": "حفظ المراسلة كمسودة",
    "CORR_SUBMIT_APPROVAL": "إرسال المراسلة للاعتماد",
    "CORR_APPROVE": "اعتماد المراسلة",
    "CORR_FINAL_REPLY": "إصدار الرد الرسمي عبر مكتب الأمين العام",
    "CORR_CLOSE": "إغلاق المراسلة",
    "CORR_ARCHIVE": "أرشفة المراسلة",
    "CORR_CONFIDENTIAL_ACCESS_UPDATE": "تحديث صلاحيات الوصول إلى مراسلة سرية",
    "GENERAL": "عام",
    "ADMIN": "مسؤول النظام",
    "SUPER_ADMIN": "مدير النظام العام",
    "SUPERADMIN": "مدير النظام العام",
    "GENERAL_SECRETARY": "الأمين العام",
    "ASSISTANT_SECRETARY_GENERAL": "مساعد الأمين العام",
    "PNCECS_HEAD": "رئيس اللجنة",
    "DIRECTORATE_HEAD": "مدير عام الإدارة",
    "DIRECTORATE_DEPUTY": "نائب مدير عام الإدارة",
    "DEPT_HEAD": "رئيس الدائرة",
    "DEPUTY_HEAD": "نائب رئيس الدائرة",
    "DIVISION_HEAD": "رئيس الشعبة",
    "DEVISION_HEAD": "رئيس الشعبة",
    "SECT_HEAD": "رئيس القسم",
    "SECTION_HEAD": "رئيس القسم",
    "EMPLOYEE": "موظف",
    "HR_ADMIN": "مسؤول الموارد البشرية",
    "ROLE": "دور وظيفي",
    "UNIT": "وحدة",
    "SECTION": "قسم",
    "DIVISION": "شعبة",
    "COMMITTEE": "لجنة",
    "ORG_NODE": "عنصر هيكلي",
    "CHAIR": "رئيس اللجنة",
    "SECRETARY": "أمين سر اللجنة",
    "MEMBER": "عضو لجنة",
    "APPROVER": "معتمد",
    "REVIEWER": "مراجع",
    "SYSTEM": "النظام",
    "INFO": "معلومة",
    "CRITICAL": "حرج",
    "OK": "مكتمل",
    "INCOMPLETE": "غير مكتمل",
    "MISSION": "مهمة عمل",
    "PUBLISHED": "منشور",
    "RATING_1_5": "تقييم من 1 إلى 5",
    "TEXT": "نص",
    "YESNO": "نعم أو لا",
    "NUMBER": "رقم",
    "SEQUENTIAL": "تسلسلي",
    "PARALLEL_SYNC": "متزامن",
    "COMMITTEE_ALL": "جميع أعضاء اللجنة",
    "ARCHIVE_DELETE": "حذف ملف من الأرشيف",
    "ARCHIVE_RESTORE": "استعادة ملف من الأرشيف",
    "ARCHIVE_SHARE": "مشاركة ملف من الأرشيف",
    "ARCHIVE_WORKFLOW_START": "بدء مسار من الأرشيف",
    "HR_LEAVE_TYPE_CREATE": "إنشاء نوع إجازة",
    "HR_ORG_ASSIGN": "إسناد موظف إلى جهة تنظيمية",
    "MEETING_MINUTES_ARCHIVE_LINK": "ربط محضر الاجتماع بالأرشيف",
    "MESSAGE_DELETED": "حذف رسالة",
    "MESSAGE_REPLY_SENT": "إرسال رد على رسالة",
    "MESSAGE_SENT": "إرسال رسالة",
    "DYNAMIC_BRANCH_SELECTED": "توجيه مسار ديناميكي إلى فرع مختار",
    "PARALLEL_SYNC_AUTHORIZED": "توجيه خطوة متزامنة إلى المعنيين",
    "PARALLEL_SYNC_BYPASS": "تجاوز مكلف في خطوة متزامنة",
    "PARALLEL_SYNC_BYPASS_ALL": "تجاوز جميع المتبقين في خطوة متزامنة",
    "PARALLEL_SYNC_RESPONDED": "تنفيذ إجراء في خطوة متزامنة",
    "PORTAL_INTEGRATION_SAVE": "حفظ إعدادات التكامل في البوابة",
    "PORTAL_PERMISSIONS_ROLE_UPDATE": "تحديث صلاحيات دور في البوابة",
    "REQUEST_DELETED": "حذف طلب",
    "REQUEST_CLOSED": "إغلاق الطلب",
    "STEP_REJECTED": "إعادة أو رفض الخطوة",
    "USER_DELETED": "حذف مستخدم",
    "USER_PROFILE_UPDATED": "تحديث الملف الشخصي للمستخدم",
    "ARCHIVE_FILE": "ملف أرشيف",
    "HR_LEAVE_TYPE": "نوع إجازة",
    "ITEM": "عنصر",
    "MESSAGE": "رسالة",
    "ORG_ASSIGN": "إسناد تنظيمي",
    "PARALLEL_TASK": "مهمة متزامنة",
    "PAYSLIP": "قسيمة راتب",
    "PORTAL_MEETING": "اجتماع في البوابة",
    "SETTING": "إعداد",
    "WORKFLOW_INSTANCE_STEP": "خطوة مسار منفذة",
    "WORKFLOW_STEP": "خطوة مسار",
    "WORKFLOW_STEP_TASK": "مهمة خطوة في مسار",
    "WORKFLOWREQUEST": "طلب مسار",
    "FIXED": "دوام ثابت",
    "FLEX": "دوام مرن",
    "SHIFT": "مناوبات",
    "RAMADAN": "دوام رمضان",
    "REMOTE": "عمل عن بُعد",
    "ONSITE": "من مقر العمل",
    "HYBRID": "عمل هجين",
    "STATUS": "حالة",
    "EXCEPTION": "استثناء",
    "VIOLATION": "مخالفة",
    "WARNING": "إنذار",
    "INVESTIGATION": "تحقيق",
    "OPEN": "مفتوح",
    "UNDER_REVIEW": "قيد المراجعة",
    "CLOSED": "مغلق",
    "LOW": "منخفض",
    "MED": "متوسط",
    "HIGH": "مرتفع",
    "URGENT": "عاجل",
    "NORMAL": "عادي",
    "SECRET": "سري",
    "POLICY": "سياسة",
    "FORM": "نموذج",
    "PROCEDURE": "إجراء",
    "NOTE": "ملاحظة",
    "HEARING": "جلسة استماع",
    "DECISION": "قرار",
    "PRESENT": "حاضر",
    "ABSENT": "غائب",
    "LEAVE": "إجازة",
    "IN": "وارد",
    "OUT": "صادر",
    "DELETE": "حذف",
    "DELETED": "محذوف",
    "DELEGATED": "مفوّض",
    "DEPT": "دائرة",
    "DEPARTMENT": "دائرة",
    "DIRECTORATE": "إدارة",
    "DOWNLOAD": "تنزيل",
    "FILES": "الملفات",
    "EMPLOYEE_FILE": "ملف موظف",
    "HR_EMPLOYEE_UPDATE": "تحديث بيانات موظف",
    "HR_PAYSLIPS_BULK_UPLOAD": "رفع قسائم الرواتب دفعة واحدة",
    "HR_PAYSLIPS_SEND": "إرسال قسائم الرواتب",
    "MINE": "ملفاتي",
    "MY_FILES": "ملفاتي",
    "OWNER": "المالك",
    "PAGE_VIEW": "عرض صفحة",
    "PORTAL_ACCESS_REQUEST_APPROVE": "اعتماد طلب صلاحية البوابة",
    "PORTAL_ACCESS_REQUEST_CREATE": "إنشاء طلب صلاحية البوابة",
    "PORTAL_ACCESS_REQUEST": "طلب صلاحية البوابة",
    "PORTAL_EMAIL_CIRCULAR_SAVE": "حفظ تعميم البريد الإلكتروني",
    "PORTAL_PERMISSIONS_USER_UPDATE": "تحديث صلاحيات مستخدم في البوابة",
    "PRIVATE": "خاص",
    "PUBLIC": "عام",
    "REQUEST": "طلب",
    "REQUEST_ESCALATION": "تنبيه على الطلب",
    "SHARE": "مشاركة",
    "SHARED": "مشارك",
    "SIGNED": "موقّع",
    "TIMECLK_SYNC": "مزامنة سجل الدوام",
    "TOTAL": "الإجمالي",
    "UNSIGNED": "غير موقّع",
    "USER": "مستخدم",
    "USER_ACTION": "تنفيذ إجراء",
    "USER_ACTION_FAILED": "محاولة إجراء غير ناجحة",
    "USER_LOGIN": "تسجيل الدخول",
    "USER_LOGOUT": "تسجيل الخروج",
    "USER_ROLE_CHANGED": "تغيير دور المستخدم",
    "UPDATE_USER": "تحديث مستخدم",
    "WORKFLOW": "مسار",
    "WORKFLOW_REQUEST": "طلب مسار",
    "WORKFLOW_STARTED": "بدء المسار",
    "WORKFLOW_REPLY": "رد على المسار",
    "WORKFLOW_MENTION_ACCESS": "إضافة مستخدم إلى المسار بالمنشن",
    "WORKFLOW_ATTACHMENT_UPLOADED": "رفع مرفق مسار",
    "MEETING_WORKFLOW_START": "بدء مسار الاجتماع",
    "STEP_APPROVED": "تمت الموافقة على الخطوة",
    "PENDING": "قيد الانتظار",
    "APPROVED": "موافق عليه",
    "REJECTED": "مرفوض",
    "IN_PROGRESS": "قيد التنفيذ",
    "BYPASSED": "تم التجاوز",
    "STEP": "خطوة",
    "DRAFT": "مسودة",
    "CANCELLED": "ملغي",
    "SUBMITTED": "مرسل",
    "ON_TRACK": "ضمن المدة",
    "BREACHED": "متجاوز المدة",
    "ESCALATED": "بحاجة إلى تنبيه",
}


MENTIONED_USER_ID_RE = re.compile(r"\bmentioned_user_id\s*=\s*(\d+)\b", flags=re.IGNORECASE)


UI_TEXT_REPLACEMENTS_AR = {
    "Admin updated user profile fields": "قام المسؤول بتحديث حقول ملف المستخدم",
    "Admin updated مستخدم profile fields": "قام المسؤول بتحديث حقول ملف المستخدم",
}


UI_TEXT_REGEX_REPLACEMENTS_AR = (
    (r"\bTIMECLK\s+sync\s+inserted=", "مزامنة جهاز الدوام: تمت إضافة="),
    (r"\bskipped=", "تم التجاهل="),
    (r"\berrors=", "الأخطاء="),
    (r"\bsummaries=", "الملخصات="),
    (r"\bAttachment\s*:", "المرفق:"),
    (r"\bfile_id=", "معرّف الملف="),
    (r"\barchived_file_id=", "معرّف الملف المؤرشف="),
    (r"\bsource=", "المصدر="),
    (r"\btemplate=", "المسار="),
    (r"\bcategory=", "التصنيف="),
    (r"\bsender=", "المرسل="),
    (r"\bcompetence=", "جهة الاختصاص="),
    (r"\bchanged\s+from\b", "تغيّر من"),
    (r"\bto\b", "إلى"),
)


def ui_label(value):
    if value is None:
        return ""
    text = str(value).strip()
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    key = re.sub(r"[\s\-]+", "_", normalized.upper())
    label = UI_LABELS_AR.get(key)
    if label:
        return label
    for phrase, phrase_label in sorted(UI_TEXT_REPLACEMENTS_AR.items(), key=lambda item: len(item[0]), reverse=True):
        if re.fullmatch(re.escape(phrase), text, flags=re.IGNORECASE):
            return phrase_label
    return text


def ui_text(value):
    if value is None:
        return ""
    text = str(value)
    for phrase, label in sorted(UI_TEXT_REPLACEMENTS_AR.items(), key=lambda item: len(item[0]), reverse=True):
        text = re.sub(re.escape(phrase), label, text, flags=re.IGNORECASE)
    text = re.sub(r"\bSTEP\s+(\d+)\b", r"الخطوة \1", text, flags=re.IGNORECASE)
    for pattern, label in UI_TEXT_REGEX_REPLACEMENTS_AR:
        text = re.sub(pattern, label, text, flags=re.IGNORECASE)
    for key, label in sorted(UI_LABELS_AR.items(), key=lambda item: len(item[0]), reverse=True):
        text = re.sub(rf"\b{re.escape(key)}\b", label, text, flags=re.IGNORECASE)

    # Older workflow mention audit records stored only the mentioned user's ID
    # in the note. Resolve those IDs at display/export time so the audit remains
    # readable without rewriting historical records.
    mentioned_ids = {int(match) for match in MENTIONED_USER_ID_RE.findall(text)}
    if mentioned_ids:
        names = {}
        try:
            from flask import g, has_request_context
            from models import User

            cache = getattr(g, "_audit_mentioned_user_names", {}) if has_request_context() else {}
            missing_ids = mentioned_ids.difference(cache)
            if missing_ids:
                users = User.query.filter(User.id.in_(missing_ids)).all()
                cache.update({int(user.id): user.full_name for user in users})
                for user_id in missing_ids:
                    cache.setdefault(int(user_id), "مستخدم غير موجود")
                if has_request_context():
                    g._audit_mentioned_user_names = cache
            names = cache
        except Exception:
            names = {}

        text = MENTIONED_USER_ID_RE.sub(
            lambda match: f"المستخدم المشار إليه={names.get(int(match.group(1)), 'مستخدم غير موجود')}",
            text,
        )
    return text
