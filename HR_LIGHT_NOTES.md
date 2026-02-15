# HR Light Module (Self-Service + Docs + Discipline)

هذه الإضافة تضيف داخل **بوابة الموارد البشرية**:
- **الطلبات الداخلية (Self‑Service)**: (شهادة/تعريف/إفادة دوام) + (تحديث بيانات) + (رفع مستندات) بسير عمل خفيف داخل HR.
- **وثائق HR**: سياسات/نماذج بإصدار معتمد + تحميل.
- **الانضباط والشؤون القانونية**: قضايا + إجراءات + مرفقات.

## 1) الصلاحيات الجديدة
تمت إضافة الصلاحيات التالية في portal/perm_defs.py:
- HR_SS_READ / HR_SS_CREATE / HR_SS_APPROVE / HR_SS_WORKFLOWS_MANAGE
- HR_DOCS_READ / HR_DOCS_MANAGE
- HR_DISCIPLINE_READ / HR_DISCIPLINE_MANAGE

> ملاحظة: شاشة صلاحيات البوابة ستُظهر هذه الصلاحيات تلقائياً من perm_defs.

## 2) إنشاء الجداول
تمت إضافة موديلات جديدة في models.py.
إذا كنت تستخدم قاعدة بيانات قديمة (SQLite) ولم تقم بعمل init جديد، قد تظهر أخطاء مثل:
`no such table ...`

الحل (بيئة تطوير):
- إمّا حذف قاعدة البيانات الحالية وإعادة init_db
- أو تشغيل init_db.py (إذا كان يقوم بـ create_all) بعد التأكد أنه ينفّذ `db.create_all()`.

## 3) إعداد سير الطلبات الداخلية
من HR Home (إذا لديك HR_SS_WORKFLOWS_MANAGE):
- اذهب إلى: **إعداد سير الطلبات الداخلية**
- ستجد تعريفات افتراضية وخطوة افتراضية (Approver Role = HR)
- يمكنك تعديل/إضافة خطوات حسب المؤسسة.

## 4) مسارات مهمة
- /portal/hr/self-service
- /portal/hr/docs
- /portal/hr/discipline
- /portal/admin/hr/self-service-workflows
