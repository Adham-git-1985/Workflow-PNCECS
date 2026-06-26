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
    "PORTAL_ACCESS_REQUEST_APPROVE": "اعتماد طلب صلاحية البوابة",
    "PORTAL_ACCESS_REQUEST_CREATE": "إنشاء طلب صلاحية البوابة",
    "PORTAL_ACCESS_REQUEST": "طلب صلاحية البوابة",
    "PORTAL_EMAIL_CIRCULAR_SAVE": "حفظ تعميم البريد الإلكتروني",
    "PORTAL_PERMISSIONS_USER_UPDATE": "تحديث صلاحيات مستخدم في البوابة",
    "PRIVATE": "خاص",
    "PUBLIC": "عام",
    "REQUEST": "طلب",
    "SHARE": "مشاركة",
    "SHARED": "مشارك",
    "SIGNED": "موقّع",
    "TIMECLK_SYNC": "مزامنة سجل الدوام",
    "TOTAL": "الإجمالي",
    "UNSIGNED": "غير موقّع",
    "USER": "مستخدم",
    "USER_ROLE_CHANGED": "تغيير دور المستخدم",
    "UPDATE_USER": "تحديث مستخدم",
    "WORKFLOW": "مسار",
    "WORKFLOW_REQUEST": "طلب مسار",
    "WORKFLOW_STARTED": "بدء المسار",
    "WORKFLOW_REPLY": "رد على المسار",
    "WORKFLOW_MENTION_ACCESS": "إضافة مستخدم إلى المسار بالمنشن",
    "WORKFLOW_ATTACHMENT_UPLOADED": "رفع مرفق مسار",
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
    return text
