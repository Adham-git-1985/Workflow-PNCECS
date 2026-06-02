# -*- coding: utf-8 -*-
"""
Arabic UI cleanup tool for Workflow-PNCECS.
Run this script from the project root after pulling the latest GitHub version.

Default behavior: dry-run only. Use --apply to modify files.
It focuses on visible UI text in HTML/Jinja and selected JavaScript labels.
It does NOT rename variables, routes, CSS classes, Bootstrap names, or internal code tokens.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path
from datetime import datetime

ROOT_MARKERS = ["app.py", "templates", "static"]
TARGET_EXTS = {".html", ".jinja", ".jinja2", ".js"}
EXCLUDE_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__", "node_modules", "migrations",
    "instance", "logs", "storage", "archive", "backups", "seed_excels", "seed excels2",
}

# Exact phrase replacements. Safe for visible labels, buttons, placeholders, titles, aria-labels, headings, table labels.
REPLACEMENTS = {
    # General navigation / UI
    "Dashboard": "لوحة التحكم",
    "Home": "الرئيسية",
    "Back": "رجوع",
    "Cancel": "إلغاء",
    "Close": "إغلاق",
    "Save": "حفظ",
    "Submit": "إرسال",
    "Search": "بحث",
    "Reset": "إعادة ضبط",
    "Filter": "تصفية",
    "Filters": "الفلاتر",
    "Clear": "مسح",
    "Actions": "الإجراءات",
    "Action": "إجراء",
    "Edit": "تعديل",
    "Delete": "حذف",
    "Remove": "إزالة",
    "View": "عرض",
    "Details": "التفاصيل",
    "Open": "فتح",
    "Print": "طباعة",
    "Export": "تصدير",
    "Import": "استيراد",
    "Download": "تنزيل",
    "Upload": "رفع",
    "New": "جديد",
    "Create": "إنشاء",
    "Update": "تحديث",
    "Add": "إضافة",
    "Manage": "إدارة",
    "Settings": "الإعدادات",
    "Help": "مساعدة",
    "Guide": "دليل",
    "Reports": "التقارير",
    "Report": "تقرير",
    "Notifications": "الإشعارات",
    "Messages": "الرسائل",
    "Inbox": "صندوق الوارد",
    "Archive": "الأرشيف",
    "Files": "الملفات",
    "File": "ملف",
    "Users": "المستخدمون",
    "User": "المستخدم",
    "Roles": "الأدوار",
    "Role": "الدور",
    "Permissions": "الصلاحيات",
    "Permission": "صلاحية",
    "Profile": "الملف الشخصي",
    "Logout": "خروج",
    "Login": "دخول",
    "Sign in": "تسجيل الدخول",
    "Sign out": "تسجيل الخروج",
    "Remember me": "تذكرني",

    # Tables / fields
    "Name": "الاسم",
    "Title": "العنوان",
    "Description": "الوصف",
    "Type": "النوع",
    "Status": "الحالة",
    "Date": "التاريخ",
    "Time": "الوقت",
    "From": "من",
    "To": "إلى",
    "By": "بواسطة",
    "Owner": "المالك",
    "Created At": "تاريخ الإنشاء",
    "Updated At": "تاريخ التحديث",
    "Created by": "أنشئ بواسطة",
    "Updated by": "حُدّث بواسطة",
    "Last update": "آخر تحديث",
    "No data": "لا توجد بيانات",
    "No records found": "لا توجد سجلات",
    "Loading": "جاري التحميل",
    "Yes": "نعم",
    "No": "لا",
    "Active": "فعّال",
    "Inactive": "غير فعّال",
    "Enabled": "مفعّل",
    "Disabled": "معطّل",

    # Workflow-specific
    "Workflow": "مسار العمل",
    "Workflow Engine": "محرك سير العمل",
    "Request": "طلب",
    "Requests": "الطلبات",
    "My Requests": "طلباتي",
    "New Request": "طلب جديد",
    "Request Type": "نوع الطلب",
    "Request Types": "أنواع الطلبات",
    "Approval": "موافقة",
    "Approvals": "الموافقات",
    "Approve": "موافقة",
    "Reject": "رفض",
    "Rejected": "مرفوض",
    "Approved": "موافق عليه",
    "Pending": "قيد الانتظار",
    "Completed": "مكتمل",
    "Draft": "مسودة",
    "Step": "الخطوة",
    "Steps": "الخطوات",
    "Route": "المسار",
    "Routes": "المسارات",
    "Routing": "التوجيه",
    "Template": "قالب",
    "Templates": "القوالب",
    "Delegation": "التفويض",
    "Escalation": "التصعيد",
    "SLA": "اتفاقية مستوى الخدمة",
    "Audit Log": "سجل التدقيق",
    "Timeline": "الخط الزمني",
    "Attachment": "مرفق",
    "Attachments": "المرفقات",

    # Portal / HR / Inventory
    "Portal": "البوابة",
    "Admin Portal": "البوابة الإدارية",
    "Human Resources": "الموارد البشرية",
    "HR": "الموارد البشرية",
    "Employee": "الموظف",
    "Employees": "الموظفون",
    "Attendance": "الحضور",
    "Leaves": "الإجازات",
    "Leave": "إجازة",
    "Payslip": "قسيمة الراتب",
    "Training": "التدريب",
    "Inventory": "المستودعات",
    "Store": "المخزن",
    "Warehouse": "المستودع",
    "Movement": "الحركة",
    "Transport": "النقل",
    "Vehicle": "مركبة",
    "Driver": "السائق",
    "Correspondence": "المراسلات",
    "Inbound": "الوارد",
    "Outbound": "الصادر",

    # Common placeholders/messages
    "Choose": "اختر",
    "Select": "اختر",
    "Please select": "يرجى الاختيار",
    "Enter": "أدخل",
    "Search or jump to": "ابحث أو انتقل إلى",
    "Type to search": "اكتب للبحث",
    "Are you sure?": "هل أنت متأكد؟",
    "Confirm": "تأكيد",
    "Success": "نجاح",
    "Error": "خطأ",
    "Warning": "تحذير",
    "Info": "معلومة",
}

# Words that should not be translated because they are technical dependencies/classes/HTML attributes.
PROTECTED_WORDS = {
    "Bootstrap", "Chart", "Chart.js", "JavaScript", "CSS", "HTML", "Python", "Flask", "SQLAlchemy",
    "SQLite", "PostgreSQL", "Windows", "Linux", "CDN", "API", "URL", "PDF", "Excel",
    "GET", "POST", "PUT", "DELETE", "PATCH", "JSON", "CSV", "UTF", "Ctrl+K",
}

# Attribute values that are visible/accessibility-facing and safe to translate.
VISIBLE_ATTRS = ("title", "placeholder", "aria-label", "alt", "data-bs-original-title")


def is_project_root(path: Path) -> bool:
    return any((path / marker).exists() for marker in ROOT_MARKERS)


def should_skip(path: Path) -> bool:
    parts = set(path.parts)
    return bool(parts & EXCLUDE_DIRS)


def replace_exact_visible_text(text: str) -> tuple[str, int]:
    count = 0
    new = text
    # Replace longer phrases first to avoid partial word replacement.
    for src, dst in sorted(REPLACEMENTS.items(), key=lambda x: len(x[0]), reverse=True):
        if src in PROTECTED_WORDS:
            continue
        pattern = re.compile(rf"(?<![A-Za-z_]){re.escape(src)}(?![A-Za-z_])")
        new, n = pattern.subn(dst, new)
        count += n
    return new, count


def translate_html_text_nodes(content: str) -> tuple[str, int]:
    """Translate visible text between HTML tags, while preserving tags/Jinja code."""
    total = 0

    def repl(match: re.Match) -> str:
        nonlocal total
        text = match.group(1)
        # Skip if the text is only whitespace or mostly Jinja/control code.
        if not text.strip() or "{%" in text or "{{" in text:
            return match.group(0)
        new_text, n = replace_exact_visible_text(text)
        total += n
        return ">" + new_text + "<"

    content = re.sub(r">([^<>]+)<", repl, content)
    return content, total


def translate_visible_attrs(content: str) -> tuple[str, int]:
    total = 0
    for attr in VISIBLE_ATTRS:
        # Double-quoted attributes
        pattern = re.compile(rf'({attr}\s*=\s*")([^"]*)(")')
        def repl_d(m: re.Match) -> str:
            nonlocal total
            new_val, n = replace_exact_visible_text(m.group(2))
            total += n
            return m.group(1) + new_val + m.group(3)
        content = pattern.sub(repl_d, content)
        # Single-quoted attributes
        pattern = re.compile(rf"({attr}\s*=\s*')([^']*)(')")
        def repl_s(m: re.Match) -> str:
            nonlocal total
            new_val, n = replace_exact_visible_text(m.group(2))
            total += n
            return m.group(1) + new_val + m.group(3)
        content = pattern.sub(repl_s, content)
    return content, total


def translate_js_strings(content: str) -> tuple[str, int]:
    """Translate exact UI phrases inside quoted JS strings only."""
    total = 0
    string_re = re.compile(r"(['\"])(.*?)(\1)", re.DOTALL)

    def repl(m: re.Match) -> str:
        nonlocal total
        quote, val, closing = m.groups()
        if len(val) > 200 or "http" in val or "url_for" in val or "class" in val:
            return m.group(0)
        new_val, n = replace_exact_visible_text(val)
        total += n
        return quote + new_val + closing

    return string_re.sub(repl, content), total


def process_file(path: Path) -> tuple[bool, int]:
    try:
        original = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return False, 0

    content = original
    total = 0

    if path.suffix.lower() in {".html", ".jinja", ".jinja2"}:
        content, n = translate_visible_attrs(content)
        total += n
        content, n = translate_html_text_nodes(content)
        total += n
    elif path.suffix.lower() == ".js":
        content, n = translate_js_strings(content)
        total += n

    if content != original:
        return True, total, content
    return False, 0, original


def main() -> int:
    parser = argparse.ArgumentParser(description="Translate visible English UI text to Arabic in Workflow-PNCECS.")
    parser.add_argument("--root", default=".", help="Project root path. Default: current directory")
    parser.add_argument("--apply", action="store_true", help="Actually write changes. Without this, only dry-run report is generated.")
    parser.add_argument("--no-backup", action="store_true", help="Do not create backup files when applying changes.")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not is_project_root(root):
        print("تحذير: المسار لا يبدو كمجلد مشروع Workflow-PNCECS. تأكد أنك داخل جذر المشروع.")

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "root": str(root),
        "mode": "apply" if args.apply else "dry-run",
        "changed_files": [],
        "total_replacements": 0,
    }

    for path in root.rglob("*"):
        if not path.is_file() or should_skip(path) or path.suffix.lower() not in TARGET_EXTS:
            continue
        changed, count, new_content = process_file(path)
        if changed:
            rel = str(path.relative_to(root))
            report["changed_files"].append({"file": rel, "replacements": count})
            report["total_replacements"] += count
            if args.apply:
                if not args.no_backup:
                    backup = path.with_suffix(path.suffix + ".bak_arabic_ui")
                    if not backup.exists():
                        shutil.copy2(path, backup)
                path.write_text(new_content, encoding="utf-8", newline="")

    report_path = root / "arabic_ui_cleanup_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("تم إنشاء التقرير:", report_path)
    print("عدد الملفات المتأثرة:", len(report["changed_files"]))
    print("عدد الاستبدالات:", report["total_replacements"])
    if not args.apply:
        print("هذه تجربة فقط. لتطبيق التعديلات فعليًا شغّل: python arabize_ui.py --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
