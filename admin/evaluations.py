from datetime import datetime
import json
import io

from flask import render_template, request, redirect, url_for, flash, send_file
from flask_login import login_required, current_user

from extensions import db
from models import AuditLog, User, EmployeeEvaluationRun, HREmployeeAchievement
from utils.perms import perm_required
from utils.excel import make_xlsx_bytes
from services.evaluation_service import (
    compute_employee_evaluation,
    compute_for_all_employees,
    import_indicator_evaluations,
    refresh_achievement_bonus_for_existing_runs,
)
from services.employee_achievements import (
    ACHIEVEMENT_LEVELS,
    ACHIEVEMENT_STATUSES,
    ACHIEVEMENT_TYPES,
    achievement_points,
)


def register_evaluation_routes(admin_bp):

    @admin_bp.route("/evaluations")
    @login_required
    @perm_required("HR_EVALUATIONS_MANAGE")
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
        pending_achievement_count = HREmployeeAchievement.query.filter_by(status="PENDING").count()

        return render_template(
            "admin/evaluations.html",
            runs=runs,
            users=users,
            now=now,
            pending_achievement_count=pending_achievement_count,
            selected={
                "period_type": period_type,
                "year": year,
                "month": month,
                "user_id": user_id,
            },
        )


    @admin_bp.route("/evaluations/achievements", methods=["GET", "POST"])
    @login_required
    @perm_required("HR_EVALUATIONS_MANAGE")
    def evaluation_achievements():
        if request.method == "POST":
            user_id = request.form.get("user_id", type=int)
            user = db.session.get(User, user_id) if user_id else None
            achievement_type = (request.form.get("achievement_type") or "OTHER").strip().upper()
            distinction_level = (request.form.get("distinction_level") or "NOTABLE").strip().upper()
            title = (request.form.get("title") or "").strip()
            description = (request.form.get("description") or "").strip()
            achieved_on = (request.form.get("achieved_on") or "").strip()
            issuer = (request.form.get("issuer") or "").strip()[:250]
            evidence_reference = (request.form.get("evidence_reference") or "").strip()[:500]

            if not user or not title or len(title) > 250:
                flash("اختر الموظف واكتب عنواناً صحيحاً للإنجاز.", "danger")
                return redirect(url_for("admin.evaluation_achievements"))
            if achievement_type not in ACHIEVEMENT_TYPES:
                achievement_type = "OTHER"
            if distinction_level not in ACHIEVEMENT_LEVELS:
                distinction_level = "NOTABLE"
            try:
                achievement_day = datetime.strptime(achieved_on, "%Y-%m-%d").date()
            except ValueError:
                flash("حدد تاريخاً صحيحاً للإنجاز.", "danger")
                return redirect(url_for("admin.evaluation_achievements"))
            if achievement_day > datetime.utcnow().date():
                flash("لا يمكن تسجيل إنجاز بتاريخ مستقبلي.", "danger")
                return redirect(url_for("admin.evaluation_achievements"))

            row = HREmployeeAchievement(
                user_id=user.id,
                achievement_type=achievement_type,
                title=title,
                description=description or None,
                achieved_on=achievement_day.isoformat(),
                issuer=issuer or None,
                evidence_reference=evidence_reference or None,
                distinction_level=distinction_level,
                evaluation_points=achievement_points(distinction_level),
                status="APPROVED",
                submitted_by_id=current_user.id,
                reviewed_by_id=current_user.id,
                reviewed_at=datetime.utcnow(),
            )
            db.session.add(row)
            db.session.flush()
            db.session.add(AuditLog(
                user_id=current_user.id,
                action="HR_ACHIEVEMENT_APPROVE",
                note=f"level={distinction_level} points={row.evaluation_points}",
                target_type="HR_EMPLOYEE_ACHIEVEMENT",
                target_id=row.id,
                created_at=datetime.utcnow(),
            ))
            db.session.commit()
            refreshed = refresh_achievement_bonus_for_existing_runs(user.id, row.achieved_on)
            flash(f"تم اعتماد الإنجاز وإضافته للتقييم. تم تحديث {refreshed} تقييم محفوظ.", "success")
            return redirect(url_for("admin.evaluation_achievements", user_id=user.id))

        selected_status = (request.args.get("status") or "").strip().upper()
        selected_user_id = request.args.get("user_id", type=int)
        selected_year = request.args.get("year", type=int)
        query = HREmployeeAchievement.query
        if selected_status in ACHIEVEMENT_STATUSES:
            query = query.filter(HREmployeeAchievement.status == selected_status)
        if selected_user_id:
            query = query.filter(HREmployeeAchievement.user_id == selected_user_id)
        if selected_year:
            query = query.filter(HREmployeeAchievement.achieved_on.like(f"{selected_year:04d}-%"))
        rows = query.order_by(
            HREmployeeAchievement.status.desc(),
            HREmployeeAchievement.achieved_on.desc(),
            HREmployeeAchievement.id.desc(),
        ).limit(1000).all()
        users = User.query.order_by(User.name.asc(), User.email.asc()).all()
        return render_template(
            "admin/evaluation_achievements.html",
            rows=rows,
            users=users,
            achievement_types=ACHIEVEMENT_TYPES,
            achievement_levels=ACHIEVEMENT_LEVELS,
            achievement_statuses=ACHIEVEMENT_STATUSES,
            selected_status=selected_status,
            selected_user_id=selected_user_id,
            selected_year=selected_year,
            now=datetime.utcnow(),
        )


    @admin_bp.route("/evaluations/achievements/<int:achievement_id>/review", methods=["POST"])
    @login_required
    @perm_required("HR_EVALUATIONS_MANAGE")
    def evaluation_achievement_review(achievement_id):
        row = HREmployeeAchievement.query.get_or_404(achievement_id)
        action = (request.form.get("action") or "").strip().lower()
        distinction_level = (request.form.get("distinction_level") or row.distinction_level).strip().upper()
        if distinction_level not in ACHIEVEMENT_LEVELS:
            distinction_level = "NOTABLE"
        if action not in {"approve", "reject"}:
            flash("إجراء المراجعة غير صحيح.", "danger")
            return redirect(url_for("admin.evaluation_achievements"))

        old_status = row.status
        row.distinction_level = distinction_level
        row.status = "APPROVED" if action == "approve" else "REJECTED"
        row.evaluation_points = achievement_points(distinction_level) if action == "approve" else 0.0
        row.review_note = (request.form.get("review_note") or "").strip() or None
        row.reviewed_by_id = current_user.id
        row.reviewed_at = datetime.utcnow()
        row.updated_at = datetime.utcnow()
        db.session.add(AuditLog(
            user_id=current_user.id,
            action="HR_ACHIEVEMENT_APPROVE" if action == "approve" else "HR_ACHIEVEMENT_REJECT",
            note=row.review_note or f"level={distinction_level} points={row.evaluation_points}",
            old_status=old_status,
            new_status=row.status,
            target_type="HR_EMPLOYEE_ACHIEVEMENT",
            target_id=row.id,
            created_at=datetime.utcnow(),
        ))
        db.session.commit()
        refreshed = refresh_achievement_bonus_for_existing_runs(row.user_id, row.achieved_on)
        message = "تم اعتماد الإنجاز" if action == "approve" else "تم رفض الإنجاز واستبعاده من التقييم"
        flash(f"{message}. تم تحديث {refreshed} تقييم محفوظ.", "success")
        return redirect(url_for("admin.evaluation_achievements", user_id=row.user_id))


    @admin_bp.route("/evaluations/run", methods=["POST"])
    @login_required
    @perm_required("HR_EVALUATIONS_MANAGE")
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


    @admin_bp.route("/evaluations/import-template.xlsx")
    @login_required
    @perm_required("HR_EVALUATIONS_MANAGE")
    def evaluations_import_template():
        headers = [
            "معرف المستخدم",
            "البريد",
            "الرقم الوظيفي",
            "نوع الفترة",
            "السنة",
            "الشهر",
            "كود المؤشر",
            "اسم المؤشر",
            "العلامة من 5",
            "العلامة من 100",
            "الوزن",
            "تفسير العلامة",
            "الدليل أو المرجع",
            "تاريخ المرجع",
            "المصدر",
        ]
        data = make_xlsx_bytes("Evaluation Import", headers, [])
        return send_file(
            io.BytesIO(data),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name="evaluation_indicators_import_template.xlsx",
        )


    @admin_bp.route("/evaluations/import", methods=["POST"])
    @login_required
    @perm_required("HR_EVALUATIONS_MANAGE")
    def evaluations_import():
        file_storage = request.files.get("file")
        if not file_storage:
            flash("اختر ملف Excel للاستيراد.", "danger")
            return redirect(url_for("admin.evaluations_index"))

        defaults = {
            "period_type": (request.form.get("period_type") or "").upper().strip(),
            "year": request.form.get("year"),
            "month": request.form.get("month"),
        }

        try:
            stats = import_indicator_evaluations(
                file_storage,
                imported_by_id=current_user.id,
                defaults=defaults,
            )
            msg = (
                f"تم استيراد {stats.get('applied', 0)} مؤشر ضمن "
                f"{stats.get('runs', 0)} تقييم. الصفوف: {stats.get('rows', 0)}"
            )
            if stats.get("errors"):
                msg += f" | أخطاء: {stats.get('errors')}"
                samples = stats.get("error_samples") or []
                if samples:
                    msg += " | " + " ؛ ".join(samples[:3])
                flash(msg, "warning")
            else:
                flash(msg, "success")
        except Exception as e:
            db.session.rollback()
            flash(f"فشل استيراد ملف التقييم: {e}", "danger")

        return redirect(url_for(
            "admin.evaluations_index",
            period_type=defaults.get("period_type") or "",
            year=defaults.get("year") or "",
            month=defaults.get("month") or "",
        ))


    @admin_bp.route("/evaluations/<int:run_id>")
    @login_required
    @perm_required("HR_EVALUATIONS_MANAGE")
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
    @perm_required("HR_EVALUATIONS_MANAGE")
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
