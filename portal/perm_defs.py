# portal/perm_defs.py
# تعريف صلاحيات البوابة الإدارية (Portal) بصيغة واضحة ومقسّمة لتجربة مستخدم أفضل.
# الهدف: نفس منطق صلاحيات "المسار" (RolePermission/UserPermission) لكن بخصوص البوابة الإدارية.

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


@dataclass(frozen=True)
class PermDef:
    key: str
    label: str
    desc: str = ""
    module: str = ""  # used by UI helpers (select-all etc.)


# Canonical (CRUD-like) keys for better UX.
# Notes:
# - *_MANAGE implies CRUD (handled by User.has_perm()).
# - *_EXPORT is separate because export is sensitive in many orgs.
PERMS: Dict[str, List[PermDef]] = {
    "الوصول والتنقل": [
        PermDef("PORTAL_READ", "الدخول للبوابة", "السماح بالدخول للبوابة الإدارية (Portal).", module="PORTAL"),
        PermDef("PORTAL_ADMIN_READ", "الدخول لقسم إدارة البوابة", "إظهار/الدخول إلى قسم إدارة البوابة.", module="PORTAL_ADMIN"),
    ],
    "إدارة الصلاحيات": [
        PermDef("PORTAL_ADMIN_PERMISSIONS_MANAGE", "إدارة صلاحيات البوابة", "تعديل صلاحيات الأدوار الخاصة بالبوابة (RolePermission).", module="PORTAL_ADMIN_PERMISSIONS"),
    ],
    "التعميمات": [
        PermDef("PORTAL_CIRCULARS_MANAGE", "إدارة التعميمات", "إصدار تعميمات وإرسال تنبيهات مستعجلة للمستخدمين.", module="PORTAL_CIRCULARS"),
    ],

    "المراسلات (الوارد/الصادر)": [
        PermDef("CORR_READ", "عرض المراسلات", "عرض صفحات الوارد/الصادر وقوائمها.", module="CORR"),
        PermDef("CORR_CREATE", "تسجيل مراسلة", "إنشاء وارد/صادر جديد.", module="CORR"),
        PermDef("CORR_UPDATE", "تعديل مراسلة", "تعديل بيانات وارد/صادر.", module="CORR"),
        PermDef("CORR_DELETE", "حذف/أرشفة مراسلة", "حذف/أرشفة مراسلات حسب السياسة.", module="CORR"),
        PermDef("CORR_EXPORT", "تصدير المراسلات", "تصدير PDF/Excel لقوائم المراسلات.", module="CORR"),
        PermDef("CORR_LOOKUPS_MANAGE", "إعدادات المراسلات", "إدارة التصنيفات والجهات (مرسل/مستلم) من لوحة الإدارة.", module="CORR_LOOKUPS"),
    ],
    "الموارد البشرية": [
        PermDef("HR_READ", "عرض الموارد البشرية", "الدخول لصفحات HR الأساسية.", module="HR"),

        PermDef("HR_REQUESTS_READ", "عرض طلباتي", "عرض طلبات الإجازات/المغادرات الخاصة بالموظف.", module="HR_REQUESTS"),
        PermDef("HR_REQUESTS_CREATE", "تقديم طلبات", "تقديم طلب إجازة/مغادرة من صفحة الموظف.", module="HR_REQUESTS"),
        PermDef("HR_REQUESTS_APPROVE", "اعتماد الطلبات", "اعتماد/رفض طلبات الموظفين (مدير مباشر/HR).", module="HR_REQUESTS"),
        PermDef("HR_REQUESTS_VIEW_ALL", "عرض جميع الطلبات", "عرض كل طلبات الموظفين (HR Admin).", module="HR_REQUESTS"),
        PermDef("HR_SS_READ", "الطلبات الداخلية (Self-Service)", "الدخول لوحدة الطلبات الداخلية (شهادة/تحديث بيانات/رفع مستندات).", module="HR_SS"),
        PermDef("HR_SS_CREATE", "تقديم طلبات داخلية", "إنشاء وتقديم طلبات داخلية للموظف.", module="HR_SS"),
        PermDef("HR_SS_APPROVE", "اعتماد الطلبات الداخلية", "اعتماد/رفض/إرجاع الطلبات الداخلية حسب الخطوات.", module="HR_SS"),
        PermDef("HR_SS_WORKFLOWS_MANAGE", "إعداد سير الطلبات الداخلية", "إدارة تعريف الخطوات والاعتمادات داخل HR.", module="HR_SS_ADMIN"),
        PermDef("HR_DISCIPLINE_READ", "عرض الانضباط والشؤون القانونية", "عرض قضايا الانضباط/التحقيقات.", module="HR_DISCIPLINE"),
        PermDef("HR_DISCIPLINE_MANAGE", "إدارة الانضباط والشؤون القانونية", "إنشاء/تحديث القضايا والإجراءات والمرفقات.", module="HR_DISCIPLINE"),
        PermDef("HR_DOCS_READ", "عرض وثائق HR", "عرض السياسات والنماذج المعتمدة.", module="HR_DOCS"),
        PermDef("HR_DOCS_MANAGE", "إدارة وثائق HR", "رفع نسخ جديدة وإدارة الإصدار المعتمد.", module="HR_DOCS"),
        PermDef("HR_PAYSLIP_VIEW", "عرض قسيمة الراتب", "إتاحة تحميل/عرض قسيمة الراتب (PDF)", module="HR_PAYSLIP"),
        PermDef("HR_PERFORMANCE_READ", "عرض الأداء والتقييم", "الدخول لوحدة الأداء والتقييم (360).", module="HR_PERF"),
        PermDef("HR_PERFORMANCE_SUBMIT", "تقديم تقييمات 360", "تعبئة تقييمات (ذاتي/زملاء/مدير) حسب التكليف.", module="HR_PERF"),
        PermDef("HR_PERFORMANCE_MANAGE", "إدارة الأداء والتقييم", "إنشاء نماذج التقييم ودورات الأداء وتوليد التكليفات.", module="HR_PERF_ADMIN"),
        PermDef("HR_PERFORMANCE_EXPORT", "تصدير تقييمات الأداء", "تصدير ملخصات ونتائج الأداء PDF/Excel.", module="HR_PERF"),
        PermDef(
            "HR_SYSTEM_EVALUATION_VIEW",
            "عرض التقييم النظامي",
            "عرض التقييم النظامي الشهري/السنوي للموظف (من 5.0). يظهر للموظف ضمن صفحة \"تقييمي النظامي\".",
            module="HR_SYS_EVAL",
        ),
        PermDef("HR_ATTENDANCE_READ", "عرض الدوام", "عرض دفعات/أحداث الدوام وتقاريرها.", module="HR_ATTENDANCE"),
        PermDef("HR_ATTENDANCE_CREATE", "استيراد الدوام", "استيراد بيانات ساعة الدوام (يدوي/تلقائي لاحقًا).", module="HR_ATTENDANCE"),
        PermDef("HR_ATTENDANCE_EXPORT", "تصدير الدوام", "تصدير الدوام PDF/Excel.", module="HR_ATTENDANCE"),
        PermDef("HR_REPORTS_VIEW", "عرض تقارير الموارد البشرية", "عرض تقارير الموارد البشرية (الإجازات/الدوام).", module="HR_REPORTS"),
        PermDef("HR_REPORTS_EXPORT", "تصدير تقارير الموارد البشرية", "تصدير تقارير الموارد البشرية PDF/Excel.", module="HR_REPORTS"),
        PermDef("HR_MASTERDATA_MANAGE", "إعدادات HR", "إدارة أنواع المغادرات/الإجازات/الجداول... من لوحة الإدارة.", module="HR_MASTERDATA"),
        PermDef("HR_EMPLOYEE_READ", "عرض ملفات الموظفين", "عرض ملف الموظف وبياناته الأساسية.", module="HR_EMPLOYEE"),
        PermDef("HR_EMPLOYEE_MANAGE", "تعديل ملفات الموظفين", "تعديل بيانات ملف الموظف وربط كود الساعة.", module="HR_EMPLOYEE"),
        PermDef("HR_EMPLOYEE_ATTACHMENTS_MANAGE", "مرفقات الموظفين", "رفع/حذف مرفقات ملف الموظف (هوية/عقد/شهادات...).", module="HR_EMPLOYEE_ATTACH"),
        PermDef("HR_ORGSTRUCTURE_READ", "عرض الهيكل التنظيمي", "عرض الهيكل التنظيمي والمدير المباشر/البديل.", module="HR_ORG"),
        PermDef("HR_ORGSTRUCTURE_MANAGE", "إدارة الهيكل التنظيمي", "تعديل المدير المباشر/البديل وإدارة الفرق (Teams).", module="HR_ORG"),
        PermDef("HR_ORG_DYNAMIC_GUIDE_VIEW", "دليل الهيكلية الموحدة", "إظهار دليل شرح الهيكلية الموحدة (Dynamic) وخياراتها وربطها بالمسارات والموافقات.", module="HR_ORG"),
        PermDef("HR_EMPLOYEE_FILES_MANAGE", "ملفات الموظفين (قديم)", "مفتاح قديم للتوافق الخلفي.", module="HR_EMPLOYEE_FILES"),
    ],
    "المستودع": [
        PermDef("STORE_READ", "عرض المستودع", "عرض ملفات المستودع.", module="STORE"),
        PermDef("STORE_MANAGE", "إدارة المستودع", "رفع/حذف/تنظيم ملفات المستودع.", module="STORE"),
        PermDef("STORE_EXPORT", "تصدير المستودع", "تصدير/تحميل جماعي حسب السياسة.", module="STORE"),
    ],
    "الحركة والنقل": [
        PermDef("TRANSPORT_READ", "عرض وحدة الحركة", "الدخول لوحدة الحركة والنقل.", module="TRANSPORT"),
        PermDef("TRANSPORT_CREATE", "إنشاء سجلات الحركة", "إنشاء أذون حركة/رحلات/مهام.", module="TRANSPORT"),
        PermDef("TRANSPORT_UPDATE", "تعديل سجلات الحركة", "تعديل بيانات السيارات/السائقين/الأذون/الرحلات.", module="TRANSPORT"),
        PermDef("TRANSPORT_DELETE", "حذف سجلات الحركة", "حذف سجلات وحدة الحركة حسب السياسة.", module="TRANSPORT"),
        PermDef("TRANSPORT_APPROVE", "اعتماد أذون الحركة", "اعتماد/رفض أذون الحركة.", module="TRANSPORT_APPROVE"),
        PermDef("TRANSPORT_TRACKING_READ", "عرض التتبع", "عرض مسارات/نقاط التتبع (عند تفعيلها).", module="TRANSPORT_TRACK"),
        PermDef("TRANSPORT_TRACKING_MANAGE", "إعدادات التتبع", "إعداد التكامل مع أجهزة تتبع المركبات (الخيار C).", module="TRANSPORT_TRACK"),
    ],

    "التقارير والتدقيق": [
        PermDef("PORTAL_REPORTS_READ", "عرض التقارير", "عرض لوحات وتقارير البوابة.", module="PORTAL_REPORTS"),
        PermDef("PORTAL_REPORTS_EXPORT", "تصدير التقارير", "تصدير تقارير البوابة PDF/Excel.", module="PORTAL_REPORTS"),
        PermDef("PORTAL_AUDIT_READ", "عرض التدقيق", "عرض سجلات التدقيق الخاصة بالبوابة.", module="PORTAL_AUDIT"),
    ],
    "الإعدادات والتكامل": [
        PermDef("PORTAL_SETTINGS_MANAGE", "إعدادات البوابة", "إدارة إعدادات البوابة.", module="PORTAL_SETTINGS"),
        PermDef("PORTAL_INTEGRATIONS_MANAGE", "تكاملات البوابة", "إعداد مزامنات/تكاملات (مثل ملف ساعة الدوام على السيرفر).", module="PORTAL_INTEGRATIONS"),
    ],
}

ALL_KEYS = [p.key for group in PERMS.values() for p in group]

# Legacy aliases used previously in project (kept for backward compatibility in User.has_perm)
ALIASES = {
    "PORTAL_VIEW": "PORTAL_READ",
    "CORR_VIEW": "CORR_READ",
    "CORR_IN_CREATE": "CORR_CREATE",
    "CORR_OUT_CREATE": "CORR_CREATE",
    "HR_ATTENDANCE_IMPORT": "HR_ATTENDANCE_CREATE",
    "HR_EMPLOYEE_FILES_MANAGE": "HR_EMPLOYEE_MANAGE",
}
