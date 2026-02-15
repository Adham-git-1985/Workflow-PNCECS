from decimal import Decimal, ROUND_HALF_UP


def clamp(n: float, lo: float, hi: float) -> float:
    try:
        n = float(n)
    except Exception:
        n = lo
    return max(lo, min(hi, n))


def score_5_from_100(score_100: float) -> float:
    """Convert 0..100 into 0..5 with rounding to nearest 0.1 (half-up)."""
    s100 = Decimal(str(clamp(score_100, 0.0, 100.0)))
    s5 = (s100 / Decimal("20")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    # cap 0.0..5.0
    if s5 < Decimal("0.0"):
        s5 = Decimal("0.0")
    if s5 > Decimal("5.0"):
        s5 = Decimal("5.0")
    return float(s5)
