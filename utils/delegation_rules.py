from datetime import date
from models import Delegation

def validate_delegation(from_user_id, to_user_id, role, start_date, end_date):
    # 1️⃣ منع التفويض للنفس
    if from_user_id == to_user_id:
        raise ValueError("لا يمكن تفويض المستخدم لنفسه")

    # 2️⃣ التواريخ
    if end_date < date.today():
        raise ValueError("تاريخ انتهاء التفويض منتهٍ")

    if start_date > end_date:
        raise ValueError("تاريخ البداية أكبر من تاريخ النهاية")

    # 3️⃣ منع التفويض المتداخل لنفس الدور
    overlapping = Delegation.query.filter(
        Delegation.from_user_id == from_user_id,
        Delegation.role == role,
        Delegation.is_active == True,
        Delegation.end_date >= start_date
    ).first()

    if overlapping:
        raise ValueError("يوجد تفويض نشط أو متداخل لنفس الصلاحية")
