# استيراد الموظفين من Excel إلى نظام Workflow_PNCECS

هذه الأداة تستورد ملف Excel إلى النظام عبر قاعدة البيانات نفسها (`instance/workflow.db` أو عبر --db).

## البريد الإلكتروني (المهم)
العمود قد يكون:
1) **إيميل كامل** مثل: `user@gmail.com` أو `user@pncecs.plo.ps` → سيتم استخدامه كما هو.
2) **اسم مستخدم فقط** (بدون @) مثل: `adham.pncecs` أو `K.hantash.k` → سيتم تركيب الإيميل تلقائياً:
- إذا كان الاسم يحتوي على أي كلمة من `--internal-hints` (افتراضياً: `pncecs`) → يستخدم `--internal-domain` (افتراضياً: `pncecs.plo.ps`)
- غير ذلك → يستخدم `--email-domain` (افتراضياً: `pncecs.plo.ps`)

✅ مثال عملي لمزيج gmail + pncecs:
- اجعل `--email-domain gmail.com`
- واجعل `--internal-domain pncecs.plo.ps`
وسيصبح:
- `adham.pncecs` → `adham.pncecs@pncecs.plo.ps`
- `K.hantash.k` → `k.hantash.k@gmail.com`

## كلمة المرور
- لتثبيت كلمة مرور واحدة لكل المستخدمين (مثلاً 123):
  استخدم `--password 123`
- إذا تريد **تعديل كلمات المرور للموجودين مسبقاً** أيضاً:
  أضف `--reset-password`

## أوامر التشغيل (CMD داخل جذر المشروع)

### 1) تجربة بدون كتابة على القاعدة (Dry-run)
```bat
python tools\import_employees_excel.py --excel "قائمة الموظفين معدلة.xlsx" --db "instance\workflow.db" --dry-run
```

### 2) استيراد فعلي (مع دومين gmail الافتراضي + pncecs داخلي) + كلمة مرور 123
```bat
python tools\import_employees_excel.py --excel "قائمة الموظفين معدلة.xlsx" --db "instance\workflow.db" --email-domain "gmail.com" --internal-domain "pncecs.plo.ps" --password 123
```

### 3) إصلاح الاستيراد السابق (تحديث الإيميل + إعادة ضبط كلمة المرور للموجودين)
```bat
python tools\import_employees_excel.py --excel "قائمة الموظفين معدلة.xlsx" --db "instance\workflow.db" --email-domain "gmail.com" --internal-domain "pncecs.plo.ps" --password 123 --update-email --reset-password
```

> ملاحظة: `--update-email` يحدث ايميل المستخدم الموجود إذا وجد في Excel (إيميل كامل أو اسم مستخدم).

## مخرجات
- `instance/import_reports/import_summary.json`
- `instance/import_reports/new_users_credentials.csv` (للمستخدمين الجدد فقط)
