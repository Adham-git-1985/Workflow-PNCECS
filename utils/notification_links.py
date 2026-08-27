"""Shared helpers for safe, request-aware notification links."""

from __future__ import annotations

from urllib.parse import urlsplit


def safe_local_notification_url(value: str | None) -> str | None:
    """Return a same-application URL, rejecting external or malformed targets."""
    url = str(value or "").strip()
    if not url or not url.startswith("/") or url.startswith("//"):
        return None
    if "\r" in url or "\n" in url or "\\" in url:
        return None
    parsed = urlsplit(url)
    if parsed.scheme or parsed.netloc:
        return None
    return url


def notification_target_path(target_type: str | None, target_id) -> str | None:
    """Build a local detail path for notification-producing domain objects."""
    try:
        object_id = int(target_id)
    except (TypeError, ValueError):
        return None
    if object_id <= 0:
        return None

    normalized = "".join(ch for ch in str(target_type or "").upper() if ch.isalnum())
    builders = {
        "WORKFLOWREQUEST": lambda value: f"/workflow/request/{value}",
        "TROUBLETICKET": lambda value: f"/portal/trouble-tickets/{value}",
        "PORTALCIRCULAR": lambda value: f"/portal/circulars/{value}",
        "PORTALACCESSREQUEST": lambda value: f"/portal/admin/access-requests/{value}",
        "PORTALMEETING": lambda value: f"/portal/meetings/{value}",
        "HRSSREQUEST": lambda value: f"/portal/hr/self-service/requests/{value}",
        "HRLEAVEREQUEST": lambda value: f"/portal/hr/approvals/leaves/{value}",
        "HRPERMISSIONREQUEST": lambda value: f"/portal/hr/approvals/permissions/{value}",
        "CORRINBOUND": lambda value: f"/portal/corr/inbound/{value}",
        "CORROUTBOUND": lambda value: f"/portal/corr/outbound/{value}",
        "HRTRAININGPROGRAM": lambda value: f"/portal/hr/training/programs/{value}/info",
        "STOREFILE": lambda value: f"/portal/store/files/{value}/view",
    }
    builder = builders.get(normalized)
    return builder(object_id) if builder else None
