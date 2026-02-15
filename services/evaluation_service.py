from __future__ import annotations

from datetime import datetime, timedelta
import json

from sqlalchemy import func, and_, or_

from extensions import db
from models import AuditLog, ArchivedFile, WorkflowRequest, SystemSetting, EmployeeEvaluationRun, User
from utils.scoring import clamp, score_5_from_100


FINAL_STATUSES = ["APPROVED", "REJECTED"]


def _get_setting_int(key: str, default: int) -> int:
    s = SystemSetting.query.filter_by(key=key).first()
    try:
        return int(s.value) if s and s.value is not None else default
    except Exception:
        return default


def get_sla_days_default() -> int:
    return _get_setting_int("SLA_DAYS", 3)


def _period_range(period_type: str, year: int, month: int | None) -> tuple[datetime, datetime]:
    period_type = (period_type or "").upper().strip()
    if period_type == "MONTHLY":
        if not month or month < 1 or month > 12:
            raise ValueError("month is required for MONTHLY")
        start = datetime(year, month, 1)
        if month == 12:
            end = datetime(year + 1, 1, 1)
        else:
            end = datetime(year, month + 1, 1)
        return start, end

    if period_type == "ANNUAL":
        start = datetime(year, 1, 1)
        end = datetime(year + 1, 1, 1)
        return start, end

    raise ValueError("period_type must be MONTHLY or ANNUAL")


def _norm_count(count: int, target: int) -> float:
    if target <= 0:
        return 0.0
    return clamp(count / float(target), 0.0, 1.0)


def compute_employee_evaluation(user_id: int, period_type: str, year: int, month: int | None, *, created_by_id: int | None = None) -> EmployeeEvaluationRun:
    """Compute and upsert an EmployeeEvaluationRun for a given period."""

    start, end = _period_range(period_type, year, month)
    sla_days = get_sla_days_default()
    sla_deadline = start  # not used; for clarity

    u = User.query.get_or_404(user_id)

    # ---------------------------
    # Base counts from AuditLog
    # ---------------------------
    base_audit_q = AuditLog.query.filter(
        AuditLog.created_at >= start,
        AuditLog.created_at < end,
        or_(AuditLog.user_id == user_id, AuditLog.on_behalf_of_id == user_id)
    )

    # exclude message noise
    base_audit_q = base_audit_q.filter(~AuditLog.action.like("MESSAGE_%"))

    total_actions = base_audit_q.count()
    approvals = base_audit_q.filter(AuditLog.action == "APPROVE").count()
    rejections = base_audit_q.filter(AuditLog.action == "REJECT").count()

    # compliance proxy: rejection note present
    rej_with_note = base_audit_q.filter(
        AuditLog.action == "REJECT",
        AuditLog.note.isnot(None),
        func.length(func.trim(AuditLog.note)) > 0
    ).count()
    rej_note_ratio = None
    if rejections > 0:
        rej_note_ratio = clamp(rej_with_note / float(rejections), 0.0, 1.0)

    # SLA proxy: decisions taken within SLA days from request creation
    decisions_q = base_audit_q.filter(AuditLog.action.in_(["APPROVE", "REJECT"]))
    decision_rows = decisions_q.with_entities(AuditLog.request_id, AuditLog.created_at).all()
    on_time = 0
    late = 0
    if decision_rows:
        req_ids = sorted({rid for rid, _ in decision_rows if rid})
        if req_ids:
            req_map = {
                rid: created_at
                for (rid, created_at) in (
                    WorkflowRequest.query
                    .filter(WorkflowRequest.id.in_(req_ids))
                    .with_entities(WorkflowRequest.id, WorkflowRequest.created_at)
                    .all()
                )
            }
            for rid, act_at in decision_rows:
                rc = req_map.get(rid)
                if not rc:
                    continue
                delta = act_at - rc
                if delta <= timedelta(days=sla_days):
                    on_time += 1
                else:
                    late += 1

    sla_ratio = None
    if (on_time + late) > 0:
        sla_ratio = clamp(on_time / float(on_time + late), 0.0, 1.0)

    # ---------------------------
    # Archive uploads (files)
    # ---------------------------
    archive_uploads = ArchivedFile.query.filter(
        ArchivedFile.owner_id == user_id,
        ArchivedFile.upload_date >= start,
        ArchivedFile.upload_date < end
    ).count()

    # Also include archive uploads logged in audit (if used)
    audit_archive_uploads = base_audit_q.filter(AuditLog.action == "ARCHIVE_UPLOADED").count()

    # ---------------------------
    # Requests created by employee (as requester)
    # ---------------------------
    created_requests = WorkflowRequest.query.filter(
        WorkflowRequest.requester_id == user_id,
        WorkflowRequest.created_at >= start,
        WorkflowRequest.created_at < end
    ).count()

    created_closed = WorkflowRequest.query.filter(
        WorkflowRequest.requester_id == user_id,
        WorkflowRequest.created_at >= start,
        WorkflowRequest.created_at < end,
        WorkflowRequest.status.in_(FINAL_STATUSES)
    ).count()

    requester_closure_ratio = None
    if created_requests > 0:
        requester_closure_ratio = clamp(created_closed / float(created_requests), 0.0, 1.0)

    # ---------------------------
    # Scoring (0..100)
    # ---------------------------
    # Targets: monthly vs annual
    if str(period_type).upper() == "ANNUAL":
        tgt_actions = 600
        tgt_decisions = 120
        tgt_files = 120
    else:
        tgt_actions = 50
        tgt_decisions = 10
        tgt_files = 10

    comp = {}

    # Always computable
    comp["activity"] = {
        "weight": 20,
        "value": total_actions,
        "norm": _norm_count(total_actions, tgt_actions)
    }

    # Decisions/productivity (only if employee did decisions)
    decisions_count = approvals + rejections
    if decisions_count > 0:
        comp["decisions"] = {
            "weight": 25,
            "value": decisions_count,
            "norm": _norm_count(decisions_count, tgt_decisions)
        }

    # SLA (only if decisions exist and we could compute it)
    if sla_ratio is not None:
        comp["sla"] = {
            "weight": 30,
            "value": sla_ratio,
            "norm": sla_ratio
        }

    # Documentation / compliance (files or reject-notes or both)
    doc_parts = []
    if archive_uploads or audit_archive_uploads:
        doc_parts.append(_norm_count(archive_uploads + audit_archive_uploads, tgt_files))
    if rej_note_ratio is not None:
        doc_parts.append(rej_note_ratio)

    if doc_parts:
        comp["documentation"] = {
            "weight": 25,
            "value": {
                "archive_uploads": archive_uploads,
                "audit_archive_uploads": audit_archive_uploads,
                "rejections": rejections,
                "rejections_with_note": rej_with_note,
                "rej_note_ratio": rej_note_ratio,
            },
            "norm": clamp(sum(doc_parts) / float(len(doc_parts)), 0.0, 1.0)
        }

    # Requester completion (only if user created requests)
    if requester_closure_ratio is not None:
        comp["requester_completion"] = {
            "weight": 10,
            "value": {
                "created_requests": created_requests,
                "created_closed": created_closed,
                "ratio": requester_closure_ratio,
            },
            "norm": requester_closure_ratio
        }

    # redistribute weights across available components
    total_w = sum(v["weight"] for v in comp.values()) or 1
    score_100 = 0.0
    for k, v in comp.items():
        w = v["weight"] / total_w
        part = 100.0 * w * clamp(v.get("norm", 0.0), 0.0, 1.0)
        v["effective_weight"] = round(100 * w, 2)
        v["score_part"] = round(part, 2)
        score_100 += part

    score_100 = round(clamp(score_100, 0.0, 100.0), 2)
    score_5 = score_5_from_100(score_100)

    # Summary (simple generated text; AI integration can replace this later)
    summary_bits = []
    summary_bits.append(f"الحركات: {total_actions}")
    if decisions_count:
        summary_bits.append(f"قرارات: {decisions_count} (موافقة {approvals} / رفض {rejections})")
    if sla_ratio is not None:
        summary_bits.append(f"الالتزام بالوقت: {int(round(sla_ratio * 100))}%")
    if archive_uploads or audit_archive_uploads:
        summary_bits.append(f"ملفات مرفوعة: {archive_uploads + audit_archive_uploads}")
    if rej_note_ratio is not None:
        summary_bits.append(f"توثيق الرفض: {int(round(rej_note_ratio * 100))}%")

    summary = " | ".join(summary_bits)

    breakdown = {
        "employee": {"id": u.id, "name": getattr(u, "name", None) or getattr(u, "username", None) or getattr(u, "email", "")},
        "period": {
            "type": period_type.upper(),
            "year": year,
            "month": month,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "sla_days": sla_days,
        },
        "metrics": {
            "total_actions": total_actions,
            "approvals": approvals,
            "rejections": rejections,
            "rejections_with_note": rej_with_note,
            "rej_note_ratio": rej_note_ratio,
            "sla_on_time": on_time,
            "sla_late": late,
            "sla_ratio": sla_ratio,
            "archive_uploads": archive_uploads,
            "audit_archive_uploads": audit_archive_uploads,
            "created_requests": created_requests,
            "created_closed": created_closed,
            "requester_closure_ratio": requester_closure_ratio,
        },
        "components": comp,
        "score": {"score_100": score_100, "score_5": score_5},
        "notes": [
            "الدرجة الأساسية تُحسب من مؤشرات رقمية (KPI) ثم تُحوّل إلى 5 مع تقريب 0.1.",
            "الالتزام بالوقت يُحتسب كمدة بين تاريخ إنشاء الطلب وتاريخ قرار الموظف (موافقة/رفض) مقارنة بSLA_DAYS.",
        ]
    }

    # Upsert
    period_type_u = period_type.upper()
    run = EmployeeEvaluationRun.query.filter_by(
        user_id=user_id,
        period_type=period_type_u,
        year=year,
        month=month if period_type_u == "MONTHLY" else None
    ).first()

    if not run:
        run = EmployeeEvaluationRun(
            user_id=user_id,
            period_type=period_type_u,
            year=year,
            month=month if period_type_u == "MONTHLY" else None,
            start_date=start,
            end_date=end,
        )
        db.session.add(run)

    run.start_date = start
    run.end_date = end
    run.score_100 = score_100
    run.score_5 = score_5
    run.breakdown_json = json.dumps(breakdown, ensure_ascii=False)
    run.summary = summary
    run.created_by_id = created_by_id
    run.created_at = datetime.utcnow()

    db.session.commit()
    return run


def compute_for_all_employees(period_type: str, year: int, month: int | None, *, created_by_id: int | None = None) -> int:
    """Compute evaluation for all active users."""
    users = User.query.order_by(User.id.asc()).all()
    count = 0
    for u in users:
        try:
            compute_employee_evaluation(u.id, period_type, year, month, created_by_id=created_by_id)
            count += 1
        except Exception:
            db.session.rollback()
            continue
    return count
