from datetime import datetime
import json
import io

from flask import render_template, request, redirect, url_for, flash, send_file
from flask_login import login_required, current_user

from extensions import db
from models import User, EmployeeEvaluationRun
from permissions import roles_required
from utils.excel import make_xlsx_bytes
from services.evaluation_service import compute_employee_evaluation, compute_for_all_employees


def register_evaluation_routes(admin_bp):

    @admin_bp.route("/evaluations")
    @login_required
    @roles_required("ADMIN")
    def evaluations_index():
        period_type = (request.args.get("period_type") or "").upper().strip()
        year = request.args.get("year", type=int)
        month = request.args.get("month", type=int)
        user_id = request.args.get("user_id", type=int)

        q = EmployeeEvaluationRun.query

        if period_type in ("MONTHLY", "ANNUAL"):
            q = q.filter(EmployeeEvaluationRun.period_type == period_type)

        if year:
            q = q.filter(EmployeeEvaluationRun.year == year)

        if month:
            q = q.filter(EmployeeEvaluationRun.month == month)

        if user_id:
            q = q.filter(EmployeeEvaluationRun.user_id == user_id)

        runs = q.order_by(EmployeeEvaluationRun.created_at.desc(), EmployeeEvaluationRun.id.desc()).limit(200).all()

        users = User.query.order_by(User.id.asc()).all()
        now = datetime.utcnow()

        return render_template(
            "admin/evaluations.html",
            runs=runs,
            users=users,
            now=now,
            selected={
                "period_type": period_type,
                "year": year,
                "month": month,
                "user_id": user_id,
            },
        )


    @admin_bp.route("/evaluations/run", methods=["POST"])
    @login_required
    @roles_required("ADMIN")
    def evaluations_run():
        period_type = (request.form.get("period_type") or "MONTHLY").upper().strip()
        year = int(request.form.get("year") or datetime.utcnow().year)
        month = request.form.get("month")
        month = int(month) if month else None

        mode = (request.form.get("mode") or "single").lower().strip()  # single / all
        user_id = request.form.get("user_id")
        user_id = int(user_id) if user_id else None

        if period_type == "MONTHLY" and not month:
            flash("اختر الشهر", "danger")
            return redirect(url_for("admin.evaluations_index"))

        if period_type not in ("MONTHLY", "ANNUAL"):
            flash("نوع فترة غير صحيح", "danger")
            return redirect(url_for("admin.evaluations_index"))

        try:
            if mode == "all":
                count = compute_for_all_employees(period_type, year, month, created_by_id=current_user.id)
                flash(f"تم تشغيل التقييم لـ {count} موظف", "success")
            else:
                if not user_id:
                    flash("اختر الموظف", "danger")
                    return redirect(url_for("admin.evaluations_index"))
                run = compute_employee_evaluation(user_id, period_type, year, month, created_by_id=current_user.id)
                flash("تم إنشاء التقييم", "success")
                return redirect(url_for("admin.evaluations_view", run_id=run.id))
        except Exception as e:
            db.session.rollback()
            flash(f"فشل تشغيل التقييم: {e}", "danger")

        return redirect(url_for("admin.evaluations_index", period_type=period_type, year=year, month=month or ""))


    @admin_bp.route("/evaluations/<int:run_id>")
    @login_required
    @roles_required("ADMIN")
    def evaluations_view(run_id):
        run = EmployeeEvaluationRun.query.get_or_404(run_id)
        breakdown = {}
        try:
            breakdown = json.loads(run.breakdown_json) if run.breakdown_json else {}
        except Exception:
            breakdown = {}

        return render_template("admin/evaluation_view.html", run=run, breakdown=breakdown)


    @admin_bp.route("/evaluations/export.xlsx")
    @login_required
    @roles_required("ADMIN")
    def evaluations_export_excel():
        period_type = (request.args.get("period_type") or "").upper().strip()
        year = request.args.get("year", type=int)
        month = request.args.get("month", type=int)

        q = EmployeeEvaluationRun.query
        if period_type in ("MONTHLY", "ANNUAL"):
            q = q.filter(EmployeeEvaluationRun.period_type == period_type)
        if year:
            q = q.filter(EmployeeEvaluationRun.year == year)
        if month:
            q = q.filter(EmployeeEvaluationRun.month == month)

        runs = q.order_by(EmployeeEvaluationRun.created_at.desc()).limit(5000).all()

        headers = [
            "ID",
            "Employee",
            "Period Type",
            "Year",
            "Month",
            "Score (5)",
            "Score (100)",
            "Created At",
            "Summary",
        ]

        rows = []
        for r in runs:
            emp_name = getattr(r.user, "name", None) or getattr(r.user, "username", None) or getattr(r.user, "email", "")
            rows.append([
                r.id,
                emp_name,
                r.period_type,
                r.year,
                r.month or "",
                r.score_5,
                r.score_100,
                r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "",
                r.summary or "",
            ])

        data = make_xlsx_bytes("Evaluations", headers, rows)
        filename = f"evaluations_{period_type or 'ALL'}_{year or 'all'}_{month or 'all'}.xlsx"
        return send_file(
            io.BytesIO(data),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=filename,
        )
