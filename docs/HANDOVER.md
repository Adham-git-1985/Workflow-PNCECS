# Handover – Workflow‑PNCECS (مسار + البوابة)

هذا الكتيّب مخصص لتسليم التشغيل (Dev/Test/Prod) ويجمع أوامر التشغيل والتهيئة وأهم المسارات.

---

## 1) المتطلبات
- Python 3.11+
- pip
- SQLite (افتراضي) أو PostgreSQL (لاحقًا)

---

## 2) تشغيل على Windows (Development)
### 2.1 إنشاء بيئة
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2.2 تهيئة قاعدة البيانات + Seed
```bash
python init_db.py
```
> ملاحظة: يتم إنشاء قاعدة البيانات في `instance/workflow.db`.

### 2.3 تشغيل التطبيق
```bash
python app.py
# أو
flask --app app run --debug
```

---

## 3) حسابات الدخول الافتراضية (بعد init_db)
- ADMIN: `admin@pncecs.org` / `123`
- SUPER_ADMIN: `superadmin@pncecs.org` / `123`
- USER: `adham.pncecs@gmail.com` / `123`

(قد تتغير حسب seed.xlsx)

---

## 4) أهم الروابط (Routes)
### مسار (Workflow)
- `/workflow/inbox` صندوق الوارد
- `/workflow/request/new` إنشاء طلب
- `/audit/timeline` سجل النشاط (Timeline)

### الأرشيف (Archive)
- `/archive/upload` رفع ملف
- `/archive/my-files` ملفاتي

### البوابة الإدارية (Portal)
- `/portal/` الصفحة الرئيسية
- `/portal/hr/org-structure` الهيكل التنظيمي
- `/portal/hr/*` بقية صفحات HR

---

## 5) إعدادات Timeclock Auto‑Sync
الهدف: قراءة ملف الدوام تلقائيًا من مسار ثابت على السيرفر بدل رفعه يدويًا.

### 5.1 المتغيرات (Environment / config.py)
- `TIME_CLOCK_FILE` : مسار ملف الدوام على السيرفر (مثال: `C:\\timeclock\\attendance.xlsx`)
- `TIME_CLOCK_SYNC_ENABLED` : `1` لتفعيل المزامنة
- `TIME_CLOCK_SYNC_INTERVAL_SECONDS` : مثل `60` للتحديث كل دقيقة

### 5.2 كيفية العمل
- عند أول Request بعد تشغيل التطبيق يتم تفعيل Job واحد (مرة واحدة فقط) للمزامنة.
- تم تجنب `before_first_request` لأن Flask 3 أزالها.

### 5.3 تشغيل يدوي (إن لزم)
- إذا كان لديك Route/زر للاستيراد اليدوي يمكنك استخدامه عند الحاجة.
- في حال رغبت بفصل المزامنة عن التطبيق (خدمة مستقلة)، يمكن نقل المنطق إلى CLI/Service لاحقًا.

---

## 6) فرز الجداول + البحث العام
### 6.1 Sorting (Server‑Side)
- كل صفحة جدول في البوابة تدعم:
  - `?sort=<col>&dir=asc|desc`
- يتم توليد الروابط من رؤوس الأعمدة.

### 6.2 بحث عام في كل الأعمدة
- كل Search box يمرر `q=<keyword>` ويتم تطبيقه على مجموعة أعمدة (OR) داخل الاستعلام.
- يمكنك توسيع الأعمدة من مكان واحد عبر helper (إن وجد) بدل تكرار الكود.

---

## 7) القوائم المنسدلة القابلة للبحث (Searchable Dropdowns)
- تم تفعيل Select2 (أو بديل مشابه) على عناصر `<select>` بحيث تستطيع الكتابة للبحث بدل النزول الطويل.
- مطبق في **مسار + البوابة** على جميع الـ dropdowns المهمة (منظمات/إدارات/دوائر/أقسام/شُعَب… إلخ).

---

## 8) إدارة الهيكل التنظيمي (Master Data)
المستويات المدعومة (مع خيارات ربط مرنة):
- Organization → Directorate → Department → Section → Division

مرونة الربط:
- Section يمكن أن يتبع Department أو Directorate.
- Division يمكن أن يتبع Section أو Department.

---

## 9) النسخ الاحتياطي Backup / Restore (SQLite)
### 9.1 Backup
أوقف التطبيق ثم انسخ الملف:
- `instance/workflow.db`

### 9.2 Restore
استبدل الملف بنفس الاسم ثم شغل التطبيق.

> إذا كان لديك DB‑WAL/SHM على Windows، انسخ أيضًا:
- `instance/workflow.db-wal`
- `instance/workflow.db-shm`

---

## 10) أخطاء شائعة وحلول سريعة
### Flask 3: before_first_request
- الخطأ:
  - `AttributeError: 'Flask' object has no attribute 'before_first_request'`
- الحل:
  - استخدام منطق تشغيل Job مرة واحدة على `before_request` أو داخل `@app.route('/')` أول دخول.

### بطء الجداول
- تأكد من وجود Indexes على أعمدة الفرز والبحث.

---

## 11) ملاحظات إنتاج (Production Notes)
- استخدم HTTPS + Reverse Proxy.
- فعل CSRF على النماذج.
- راجع صلاحيات رفع الملفات ومجلد التخزين.

