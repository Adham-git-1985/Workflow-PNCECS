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
    "REQUEST_ESCALATION": "تصعيد الطلب",
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
    "ON_TRACK": "ضمن المسار",
    "BREACHED": "متجاوز",
    "ESCALATED": "مصعّد",
}


MENTIONED_USER_ID_RE = re.compile(r"\bmentioned_user_id\s*=\s*(\d+)\b", flags=re.IGNORECASE)


UI_TEXT_REPLACEMENTS_AR = {
    "Admin updated user profile fields": "قام المسؤول بتحديث حقول ملف المستخدم",
    "Admin updated مستخدم profile fields": "قام المسؤول بتحديث حقول ملف المستخدم",
}


def ui_label(value):
    if value is None:
        return ""
    text = str(value).strip()
    key = text.upper().replace(" ", "_")
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
