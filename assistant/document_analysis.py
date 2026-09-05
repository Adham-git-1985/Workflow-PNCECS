"""In-memory, local-only extraction for Aref attachment analysis."""

from __future__ import annotations

from typing import Any

from flask import current_app

from services.correspondence_intake import (
    CorrespondenceIntakeError,
    OcrConfig,
    extract_attachment_text,
    read_limited_upload,
)


def _int_setting(key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(current_app.config.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _float_setting(key: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(current_app.config.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _enabled_setting(key: str, default: bool = True) -> bool:
    value = current_app.config.get(key, default)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def assistant_ocr_config() -> OcrConfig:
    """Build a bounded OCR configuration dedicated to Aref uploads."""
    return OcrConfig(
        enabled=_enabled_setting("ASSISTANT_ANALYSIS_OCR_ENABLED", True),
        command=str(
            current_app.config.get("ASSISTANT_ANALYSIS_TESSERACT_CMD", "tesseract")
            or "tesseract"
        ).strip(),
        languages=str(
            current_app.config.get("ASSISTANT_ANALYSIS_OCR_LANGUAGES", "ara+eng")
            or "ara+eng"
        ).strip(),
        max_pages=_int_setting("ASSISTANT_ANALYSIS_OCR_MAX_PAGES", 10, 1, 50),
        dpi=_int_setting("ASSISTANT_ANALYSIS_OCR_DPI", 200, 120, 400),
        timeout_seconds=_float_setting(
            "ASSISTANT_ANALYSIS_OCR_TIMEOUT_SECONDS", 45.0, 5.0, 300.0
        ),
        max_image_pixels=_int_setting(
            "ASSISTANT_ANALYSIS_OCR_MAX_IMAGE_PIXELS", 40_000_000, 1_000_000, 100_000_000
        ),
    )


def analyze_uploaded_attachment(file_storage) -> dict[str, Any]:
    """Read and extract one attachment without writing it to disk."""
    max_bytes = _int_setting(
        "ASSISTANT_ANALYSIS_MAX_FILE_BYTES", 20 * 1024 * 1024, 1_024, 100 * 1024 * 1024
    )
    max_chars = _int_setting("ASSISTANT_ANALYSIS_MAX_TEXT_CHARS", 60_000, 1_000, 200_000)
    max_pdf_pages = _int_setting("ASSISTANT_ANALYSIS_MAX_PDF_PAGES", 40, 1, 100)
    payload = read_limited_upload(file_storage, max_bytes)
    filename = str(getattr(file_storage, "filename", "") or "attachment")[:255]
    return extract_attachment_text(
        payload,
        filename,
        max_chars=max_chars,
        max_pdf_pages=max_pdf_pages,
        ocr_config=assistant_ocr_config(),
    )


__all__ = [
    "CorrespondenceIntakeError",
    "analyze_uploaded_attachment",
]
