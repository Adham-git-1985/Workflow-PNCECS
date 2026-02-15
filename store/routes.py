from __future__ import annotations

from flask import render_template
from flask_login import login_required

from utils.perms import perm_required

from . import store_bp


def _coming_soon(title: str, hint: str | None = None):
    return render_template(
        "store/coming_soon.html",
        title=title,
        hint=hint
        or "هذه الشاشة قيد التطوير حالياً. عند تزويدي بتفاصيل الشاشة سنقوم بتنفيذها ضمن نفس الترتيب.",
    )


# -------------------------
# Home
# -------------------------
@store_bp.route("/")
@login_required
@perm_required("STORE_READ")
def index():
    return render_template("store/index.html")


# -------------------------
# الطلبات
# -------------------------
@store_bp.route("/requests")
@login_required
@perm_required("STORE_READ")
def requests_log():
    return _coming_soon("سجل الطلبات")


# -------------------------
# السندات
# -------------------------
@store_bp.route("/vouchers/issue/new")
@login_required
@perm_required("STORE_MANAGE")
def voucher_issue_new():
    return _coming_soon("إدخال سند صرف")


@store_bp.route("/vouchers/issue")
@login_required
@perm_required("STORE_READ")
def voucher_issue_list():
    return _coming_soon("سجل سندات الصرف")


@store_bp.route("/vouchers/in/new")
@login_required
@perm_required("STORE_MANAGE")
def voucher_in_new():
    return _coming_soon("إدخال سند إدخال")


@store_bp.route("/vouchers/in")
@login_required
@perm_required("STORE_READ")
def voucher_in_list():
    return _coming_soon("سجل سندات الإدخال")


@store_bp.route("/vouchers/inventory/new")
@login_required
@perm_required("STORE_MANAGE")
def voucher_inventory_new():
    return _coming_soon("إدخال سند جرد")


@store_bp.route("/vouchers/inventory")
@login_required
@perm_required("STORE_READ")
def voucher_inventory_list():
    return _coming_soon("سجل سندات الجرد")


@store_bp.route("/vouchers/disposal")
@login_required
@perm_required("STORE_READ")
def voucher_disposal_list():
    return _coming_soon("سندات الإتلاف")


@store_bp.route("/vouchers/return")
@login_required
@perm_required("STORE_READ")
def voucher_return_list():
    return _coming_soon("سندات الإرجاع")


# -------------------------
# التقارير
# -------------------------
@store_bp.route("/reports/warehouses")
@login_required
@perm_required("STORE_READ")
def report_warehouses_contents():
    return _coming_soon("تقرير المخازن ومحتوياتها")


@store_bp.route("/reports/items-all")
@login_required
@perm_required("STORE_READ")
def report_items_all_warehouses():
    return _coming_soon("تقرير الأصناف والكميات في كافة المخازن")


@store_bp.route("/reports/item-card")
@login_required
@perm_required("STORE_READ")
def report_item_card():
    return _coming_soon("تقرير بطاقة الصنف")


@store_bp.route("/reports/yearly-vouchers")
@login_required
@perm_required("STORE_READ")
def report_yearly_vouchers():
    return _coming_soon("الجدول السنوي للسندات")


@store_bp.route("/reports/yearly-qty")
@login_required
@perm_required("STORE_READ")
def report_yearly_quantities():
    return _coming_soon("الجدول السنوي للكميات")


@store_bp.route("/reports/issue-logs")
@login_required
@perm_required("STORE_READ")
def report_issue_logs():
    return _coming_soon("تقرير سجلات الصرف")


# -------------------------
# العهدة
# -------------------------
@store_bp.route("/custody/inventory-voucher")
@login_required
@perm_required("STORE_READ")
def custody_inventory_voucher():
    return _coming_soon("سند جرد عهدة")


@store_bp.route("/custody/vouchers")
@login_required
@perm_required("STORE_READ")
def custody_vouchers_log():
    return _coming_soon("سجل سندات العهدة")


@store_bp.route("/custody/view")
@login_required
@perm_required("STORE_READ")
def custody_view():
    return _coming_soon("عرض العهدة")


@store_bp.route("/custody/items")
@login_required
@perm_required("STORE_READ")
def custody_items():
    return _coming_soon("الأصناف والعهدة")


# -------------------------
# لوحة التحكم
# -------------------------
@store_bp.route("/admin/constants")
@login_required
@perm_required("STORE_MANAGE")
def admin_constants():
    return _coming_soon("ثوابت النظام")


@store_bp.route("/admin/items/new")
@login_required
@perm_required("STORE_MANAGE")
def admin_items_new():
    return _coming_soon("إضافة/تعديل الأصناف")


@store_bp.route("/admin/items")
@login_required
@perm_required("STORE_MANAGE")
def admin_items_list():
    return _coming_soon("سجل الأصناف (استيراد/تصدير)")


@store_bp.route("/admin/categories")
@login_required
@perm_required("STORE_MANAGE")
def admin_categories():
    return _coming_soon("إدارة التصنيفات")


@store_bp.route("/admin/room-requesters")
@login_required
@perm_required("STORE_MANAGE")
def admin_room_requesters():
    return _coming_soon("المستخدمين القادرين على طلب مواد للغرف")


@store_bp.route("/admin/store-permissions")
@login_required
@perm_required("STORE_MANAGE")
def admin_store_permissions():
    return _coming_soon("صلاحيات الموظفين على المخازن")


@store_bp.route("/admin/settings")
@login_required
@perm_required("STORE_MANAGE")
def admin_general_settings():
    return _coming_soon("الإعدادات العامة")
