from flask import g, has_request_context, request
from flask_login import current_user


def _safe_text(value, max_len=255):
    """Return a compact single-line string suitable for audit notes."""
    try:
        value = str(value or "").strip()
    except Exception:
        value = ""
    value = " ".join(value.split())
    if max_len and len(value) > max_len:
        return value[: max_len - 3].rstrip() + "..."
    return value


def get_client_ip() -> str:
    """Best-effort client IP, including IIS/reverse-proxy headers."""
    if not has_request_context():
        return ""

    # IIS/Reverse Proxy/Load balancers commonly use X-Forwarded-For.
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        # X-Forwarded-For can contain a comma-separated chain. The first one is the original client.
        return _safe_text(forwarded_for.split(",")[0], 64)

    real_ip = request.headers.get("X-Real-IP", "")
    if real_ip:
        return _safe_text(real_ip, 64)

    return _safe_text(request.remote_addr, 64)


def _guess_browser(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if "edg/" in ua or "edge/" in ua:
        return "Microsoft Edge"
    if "opr/" in ua or "opera" in ua:
        return "Opera"
    if "chrome/" in ua and "chromium" not in ua:
        return "Chrome"
    if "firefox/" in ua:
        return "Firefox"
    if "safari/" in ua and "chrome/" not in ua:
        return "Safari"
    if "msie" in ua or "trident/" in ua:
        return "Internet Explorer"
    return "غير محدد"


def _guess_os_device(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if "windows" in ua:
        return "Windows"
    if "android" in ua:
        return "Android"
    if "iphone" in ua:
        return "iPhone"
    if "ipad" in ua:
        return "iPad"
    if "mac os" in ua or "macintosh" in ua:
        return "macOS"
    if "linux" in ua:
        return "Linux"
    return "غير محدد"


def get_audit_source_summary() -> str:
    """Human-readable audit source line for the timeline/details."""
    if not has_request_context():
        return ""

    ip = get_client_ip()
    user_agent = _safe_text(request.headers.get("User-Agent", ""), 300)
    browser = _guess_browser(user_agent)
    device_os = _guess_os_device(user_agent)
    path = _safe_text(getattr(request, "path", ""), 160)

    parts = []
    if ip:
        parts.append(f"IP={ip}")
    if device_os or browser:
        parts.append(f"الجهاز={device_os} / {browser}")
    if path:
        parts.append(f"الصفحة={path}")

    if not parts:
        return ""
    return "مصدر العملية: " + " | ".join(parts)


def delegation_audit_fields() -> dict:
    """Returns extra AuditLog fields when user is acting via delegation."""
    try:
        d = getattr(g, "delegation", None)
        eff = getattr(g, "effective_user", None)
        if d and eff and getattr(current_user, "is_authenticated", False):
            if getattr(eff, "id", None) and eff.id != current_user.id:
                return {"on_behalf_of_id": eff.id, "delegation_id": d.id}
    except Exception:
        pass
    return {}
