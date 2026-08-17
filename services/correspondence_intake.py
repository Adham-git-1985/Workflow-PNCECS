"""Local extraction and suggestions for manually uploaded correspondence.

The intake pipeline deliberately stays deterministic and local: uploaded bytes
are inspected in memory, converted to text, and matched against the live
correspondence lookups supplied by the caller.  Nothing in this module calls an
external AI service or persists the uploaded file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from io import BytesIO
from pathlib import Path
import html
import os
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
import zipfile


SMART_INTAKE_EXTENSIONS = frozenset({
    ".csv",
    ".docx",
    ".eml",
    ".htm",
    ".html",
    ".json",
    ".pdf",
    ".pptx",
    ".txt",
    ".xlsx",
    ".xml",
})

IMAGE_EXTENSIONS = frozenset({
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
})

_ARABIC_DIACRITICS = re.compile(
    r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]"
)
_MEANINGLESS_TOKENS = {
    "اداره",
    "الاداره",
    "دائره",
    "الدائره",
    "قسم",
    "القسم",
    "شعبه",
    "الشعبه",
    "فريق",
    "الفريق",
    "مسار",
    "المسار",
    "وحده",
    "الوحده",
}


class CorrespondenceIntakeError(ValueError):
    """A safe, user-facing smart-intake error."""

    def __init__(self, message: str, *, code: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class OcrConfig:
    """Bounded local OCR settings for Tesseract."""

    enabled: bool = False
    command: str = "tesseract"
    languages: str = "ara+eng"
    max_pages: int = 10
    dpi: int = 200
    timeout_seconds: float = 45.0
    max_image_pixels: int = 40_000_000


class _OcrRuntimeError(RuntimeError):
    pass


def _bounded_ocr_config(config: OcrConfig | None) -> OcrConfig:
    config = config or OcrConfig()
    languages = re.sub(r"[^A-Za-z0-9_+\-]", "", str(config.languages or ""))
    return OcrConfig(
        enabled=bool(config.enabled),
        command=str(config.command or "tesseract").strip() or "tesseract",
        languages=languages or "ara+eng",
        max_pages=max(1, min(int(config.max_pages or 1), 50)),
        dpi=max(120, min(int(config.dpi or 200), 400)),
        timeout_seconds=max(5.0, min(float(config.timeout_seconds or 45), 300.0)),
        max_image_pixels=max(1_000_000, min(int(config.max_image_pixels or 1), 100_000_000)),
    )


def _resolve_tesseract_command(config: OcrConfig) -> str | None:
    if not config.enabled:
        return None
    command = config.command
    if os.path.isfile(command):
        return os.path.abspath(command)
    return shutil.which(command)


def _image_to_png_frames(payload: bytes, config: OcrConfig) -> list[bytes]:
    """Normalize bounded image frames before handing them to OCR."""
    try:
        from PIL import Image, ImageOps, ImageSequence

        image = Image.open(BytesIO(payload))
    except Exception as exc:
        raise CorrespondenceIntakeError(
            "تعذر فتح الصورة للتحليل.", code="INVALID_IMAGE", status_code=422
        ) from exc

    frames: list[bytes] = []
    try:
        for frame_number, source_frame in enumerate(ImageSequence.Iterator(image), start=1):
            if frame_number > config.max_pages:
                break
            frame = ImageOps.exif_transpose(source_frame.copy())
            width, height = frame.size
            if width <= 0 or height <= 0 or width * height > config.max_image_pixels:
                raise CorrespondenceIntakeError(
                    "أبعاد الصورة أكبر من الحد الآمن للتحليل.",
                    code="IMAGE_TOO_LARGE",
                    status_code=413,
                )
            frame = ImageOps.grayscale(frame)
            if max(frame.size) < 1800:
                scale = min(3.0, 1800.0 / max(frame.size))
                frame = frame.resize(
                    (max(1, int(frame.width * scale)), max(1, int(frame.height * scale))),
                    Image.Resampling.LANCZOS,
                )
            frame = ImageOps.autocontrast(frame)
            output = BytesIO()
            frame.save(output, format="PNG", optimize=True)
            frames.append(output.getvalue())
    except CorrespondenceIntakeError:
        raise
    except Exception as exc:
        raise CorrespondenceIntakeError(
            "تعذر تجهيز الصورة للتحليل.", code="INVALID_IMAGE", status_code=422
        ) from exc
    finally:
        image.close()
    return frames


def _run_tesseract_png(
    png_payload: bytes,
    command: str,
    config: OcrConfig,
    timeout_seconds: float,
) -> str:
    """Run Tesseract without a shell and return its UTF-8 output."""
    with tempfile.TemporaryDirectory(prefix="corr_ocr_") as temporary_directory:
        image_path = os.path.join(temporary_directory, "page.png")
        with open(image_path, "wb") as image_file:
            image_file.write(png_payload)
        try:
            completed = subprocess.run(
                [
                    command,
                    image_path,
                    "stdout",
                    "-l",
                    config.languages,
                    "--psm",
                    "6",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=max(1.0, timeout_seconds),
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise _OcrRuntimeError("انتهت مهلة OCR قبل اكتمال التحليل.") from exc
        except OSError as exc:
            raise _OcrRuntimeError("تعذر تشغيل محرك OCR المحلي.") from exc

    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        if "failed loading language" in detail.casefold() or "error opening data file" in detail.casefold():
            raise _OcrRuntimeError(
                "حزم لغات OCR المطلوبة غير مثبتة (العربية والإنجليزية)."
            )
        raise _OcrRuntimeError("فشل محرك OCR في قراءة المرفق.")
    return completed.stdout.decode("utf-8", errors="replace")


def _ocr_png_frames(
    frames: list[bytes],
    config: OcrConfig,
    *,
    command: str,
) -> tuple[str, list[str]]:
    parts: list[str] = []
    warnings: list[str] = []
    deadline = time.monotonic() + config.timeout_seconds
    for frame_number, png_payload in enumerate(frames[:config.max_pages], start=1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            warnings.append("توقّف OCR بعد بلوغ المهلة الإجمالية للتحليل.")
            break
        try:
            page_text = _run_tesseract_png(png_payload, command, config, remaining)
        except _OcrRuntimeError as exc:
            warnings.append(str(exc))
            break
        if page_text.strip():
            parts.append(page_text)
    return "\n".join(parts), warnings


def _human_size_limit(max_bytes: int) -> str:
    """Return a concise Arabic label for a configured byte limit."""
    if max_bytes >= 1024 * 1024:
        return f"{max_bytes / (1024 * 1024):g} ميجابايت"
    if max_bytes >= 1024:
        return f"{max_bytes / 1024:g} كيلوبايت"
    return f"{max_bytes} بايت"


def read_limited_upload(file_storage, max_bytes: int) -> bytes:
    """Read an upload without allowing its compressed size to exceed the limit."""
    if not file_storage:
        raise CorrespondenceIntakeError(
            "اختر مرفقاً لتحليله.", code="MISSING_FILE", status_code=400
        )

    try:
        declared_size = int(getattr(file_storage, "content_length", 0) or 0)
    except (TypeError, ValueError):
        declared_size = 0
    if declared_size > max_bytes:
        raise CorrespondenceIntakeError(
            f"حجم المرفق أكبر من الحد المسموح للتحليل الذكي "
            f"({_human_size_limit(max_bytes)}). يمكنك حفظه كمرفق دون تحليله.",
            code="FILE_TOO_LARGE",
            status_code=413,
        )

    stream = getattr(file_storage, "stream", file_storage)
    try:
        stream.seek(0)
    except (AttributeError, OSError):
        pass

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = stream.read(min(64 * 1024, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise CorrespondenceIntakeError(
                f"حجم المرفق أكبر من الحد المسموح للتحليل الذكي "
                f"({_human_size_limit(max_bytes)}). يمكنك حفظه كمرفق دون تحليله.",
                code="FILE_TOO_LARGE",
                status_code=413,
            )

    payload = b"".join(chunks)
    if not payload:
        raise CorrespondenceIntakeError(
            "المرفق فارغ ولا يمكن تحليله.", code="EMPTY_FILE", status_code=400
        )
    return payload


def _safe_filename(filename: str | None) -> str:
    name = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name or "attachment"


def _normalize(value: str | None) -> str:
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = _ARABIC_DIACRITICS.sub("", value)
    value = (
        value.replace("أ", "ا")
        .replace("إ", "ا")
        .replace("آ", "ا")
        .replace("ى", "ي")
        .replace("ؤ", "و")
        .replace("ئ", "ي")
        .replace("ة", "ه")
    )
    value = re.sub(r"[^\w\u0600-\u06FF]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip().casefold()


def _clean_text(value: str | None, max_chars: int) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).replace("\x00", "")
    lines: list[str] = []
    previous_blank = False
    for raw_line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = re.sub(r"[\t \u00A0]+", " ", raw_line).strip()
        if not line:
            if lines and not previous_blank:
                lines.append("")
            previous_blank = True
            continue
        lines.append(line)
        previous_blank = False
        if sum(len(item) + 1 for item in lines) >= max_chars:
            break
    return "\n".join(lines).strip()[:max_chars]


def _decode_text(payload: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "cp1256"):
        try:
            return payload.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return payload.decode("utf-8", errors="replace")


def _preflight_zip(payload: bytes, *, max_uncompressed_bytes: int = 50 * 1024 * 1024) -> None:
    """Reject malformed, encrypted, or excessively expanded Office packages."""
    try:
        with zipfile.ZipFile(BytesIO(payload)) as package:
            total = 0
            for info in package.infolist():
                if info.flag_bits & 0x1:
                    raise CorrespondenceIntakeError(
                        "لا يمكن تحليل ملف Office مشفّر.",
                        code="ENCRYPTED_FILE",
                        status_code=422,
                    )
                total += max(0, int(info.file_size or 0))
                if total > max_uncompressed_bytes:
                    raise CorrespondenceIntakeError(
                        "حجم محتويات ملف Office بعد فك الضغط أكبر من الحد الآمن.",
                        code="EXPANDED_FILE_TOO_LARGE",
                        status_code=413,
                    )
    except CorrespondenceIntakeError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise CorrespondenceIntakeError(
            "ملف Office تالف أو ليس بالصيغة المتوقعة.",
            code="INVALID_OFFICE_FILE",
            status_code=422,
        ) from exc


def _extract_pdf(
    payload: bytes,
    max_chars: int,
    max_pages: int,
    ocr_config: OcrConfig | None = None,
) -> tuple[str, list[str], dict]:
    ocr_config = _bounded_ocr_config(ocr_config)
    tesseract_command = _resolve_tesseract_command(ocr_config)
    try:
        import fitz

        document = fitz.open(stream=payload, filetype="pdf")
    except Exception as exc:
        raise CorrespondenceIntakeError(
            "تعذر فتح ملف PDF. قد يكون تالفاً أو محمياً بكلمة مرور.",
            code="INVALID_PDF",
            status_code=422,
        ) from exc

    warnings: list[str] = []
    if getattr(document, "needs_pass", False):
        document.close()
        raise CorrespondenceIntakeError(
            "ملف PDF محمي بكلمة مرور ولا يمكن تحليله.",
            code="ENCRYPTED_FILE",
            status_code=422,
        )

    page_count = int(getattr(document, "page_count", 0) or 0)
    if page_count > max_pages:
        warnings.append(f"تم تحليل أول {max_pages} صفحة فقط من أصل {page_count} صفحة.")

    parts: list[str] = []
    current_length = 0
    ocr_attempted_pages = 0
    ocr_pages = 0
    scanned_pages = 0
    ocr_deadline = time.monotonic() + ocr_config.timeout_seconds
    ocr_failure_reported = False
    ocr_oversize_reported = False
    try:
        for page_number in range(min(page_count, max_pages)):
            page = document.load_page(page_number)
            page_text = page.get_text("text") or ""
            if not page_text.strip():
                scanned_pages += 1
                if (
                    tesseract_command
                    and ocr_attempted_pages < ocr_config.max_pages
                    and not ocr_failure_reported
                ):
                    remaining = ocr_deadline - time.monotonic()
                    if remaining <= 0:
                        warnings.append("توقّف OCR بعد بلوغ المهلة الإجمالية للتحليل.")
                        ocr_failure_reported = True
                    else:
                        try:
                            estimated_width = max(1, int(page.rect.width * ocr_config.dpi / 72))
                            estimated_height = max(1, int(page.rect.height * ocr_config.dpi / 72))
                            if (
                                estimated_width * estimated_height
                                > ocr_config.max_image_pixels
                            ):
                                if not ocr_oversize_reported:
                                    warnings.append(
                                        "تجاوزت أبعاد إحدى صفحات PDF الحد الآمن لـOCR؛ تم تخطيها."
                                    )
                                    ocr_oversize_reported = True
                                continue
                            ocr_attempted_pages += 1
                            pixmap = page.get_pixmap(
                                dpi=ocr_config.dpi,
                                colorspace=fitz.csGRAY,
                                alpha=False,
                            )
                            page_text = _run_tesseract_png(
                                pixmap.tobytes("png"),
                                tesseract_command,
                                ocr_config,
                                remaining,
                            )
                            if page_text.strip():
                                ocr_pages += 1
                        except _OcrRuntimeError as exc:
                            warnings.append(str(exc))
                            ocr_failure_reported = True
                        except Exception:
                            warnings.append("تعذر تجهيز إحدى صفحات PDF لإجراء OCR.")
                            ocr_failure_reported = True
            if page_text.strip():
                parts.append(page_text)
                current_length += len(page_text)
            if current_length >= max_chars:
                warnings.append("تم اختصار النص المستخرج إلى الحد الآمن للتحليل.")
                break
    finally:
        document.close()

    text = _clean_text("\n".join(parts), max_chars)
    if not text:
        if ocr_config.enabled and not tesseract_command:
            warnings.append(
                "ملف PDF ممسوح ضوئياً، لكن محرك Tesseract المحلي غير متوفر؛ أدخل البيانات يدوياً أو ثبّت OCR."
            )
        else:
            warnings.append(
                "لم يُعثر على نص داخل PDF بعد التحليل؛ راجع جودة المسح أو أدخل البيانات يدوياً."
            )
    elif ocr_pages:
        warnings.append(f"تم استخدام OCR المحلي لاستخراج النص من {ocr_pages} صفحة ممسوحة.")
    if scanned_pages > ocr_attempted_pages and ocr_attempted_pages >= ocr_config.max_pages:
        warnings.append(f"تم تطبيق OCR على أول {ocr_config.max_pages} صفحة ممسوحة فقط.")
    return text, warnings, {
        "page_count": page_count,
        "ocr_enabled": ocr_config.enabled,
        "ocr_available": bool(tesseract_command),
        "ocr_used": bool(ocr_pages),
        "ocr_pages": ocr_pages,
        "ocr_attempted_pages": ocr_attempted_pages,
    }


def _extract_image_with_ocr(
    payload: bytes,
    max_chars: int,
    ocr_config: OcrConfig | None,
) -> tuple[str, list[str], dict]:
    ocr_config = _bounded_ocr_config(ocr_config)
    command = _resolve_tesseract_command(ocr_config)
    if not ocr_config.enabled:
        return "", [
            "OCR المحلي غير مفعّل؛ استخدم اسم الملف كمقترح وأدخل بقية البيانات يدوياً."
        ], {
            "ocr_enabled": False,
            "ocr_available": False,
            "ocr_used": False,
            "ocr_pages": 0,
        }
    if not command:
        return "", [
            "محرك Tesseract المحلي غير متوفر؛ أدخل البيانات يدوياً أو ثبّت OCR العربي."
        ], {
            "ocr_enabled": True,
            "ocr_available": False,
            "ocr_used": False,
            "ocr_pages": 0,
        }

    frames = _image_to_png_frames(payload, ocr_config)
    text, warnings = _ocr_png_frames(frames, ocr_config, command=command)
    text = _clean_text(text, max_chars)
    if text:
        warnings.append(
            f"تم استخدام OCR المحلي لاستخراج النص من {len(frames)} صورة/صفحة."
        )
    else:
        warnings.append(
            "لم يُعثر على نص واضح في الصورة بعد OCR؛ راجع جودة الصورة أو أدخل البيانات يدوياً."
        )
    return text, warnings, {
        "ocr_enabled": True,
        "ocr_available": True,
        "ocr_used": bool(text),
        "ocr_pages": len(frames) if text else 0,
    }


def _extract_docx(payload: bytes, max_chars: int) -> tuple[str, list[str], dict]:
    _preflight_zip(payload)
    try:
        from docx import Document

        document = Document(BytesIO(payload))
    except Exception as exc:
        raise CorrespondenceIntakeError(
            "تعذر قراءة ملف Word.", code="INVALID_DOCX", status_code=422
        ) from exc

    parts = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
    for table in document.tables[:30]:
        for row in table.rows[:200]:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
            if sum(len(item) + 1 for item in parts) >= max_chars:
                break

    properties = document.core_properties
    metadata = {
        "subject": (properties.subject or properties.title or "").strip(),
        "sender": (properties.author or "").strip(),
    }
    return _clean_text("\n".join(parts), max_chars), [], metadata


def _extract_xlsx(payload: bytes, max_chars: int) -> tuple[str, list[str], dict]:
    _preflight_zip(payload)
    try:
        from openpyxl import load_workbook

        workbook = load_workbook(BytesIO(payload), read_only=True, data_only=True)
    except Exception as exc:
        raise CorrespondenceIntakeError(
            "تعذر قراءة ملف Excel.", code="INVALID_XLSX", status_code=422
        ) from exc

    parts: list[str] = []
    warnings: list[str] = []
    try:
        worksheets = workbook.worksheets[:5]
        if len(workbook.worksheets) > len(worksheets):
            warnings.append("تم تحليل أول خمس أوراق عمل فقط.")
        for sheet in worksheets:
            parts.append(f"[{sheet.title}]")
            for row_number, row in enumerate(
                sheet.iter_rows(min_row=1, max_row=200, max_col=40, values_only=True),
                start=1,
            ):
                values = [str(value).strip() for value in row if value not in (None, "")]
                if values:
                    parts.append(" | ".join(values))
                if row_number >= 200 or sum(len(item) + 1 for item in parts) >= max_chars:
                    break
            if sum(len(item) + 1 for item in parts) >= max_chars:
                warnings.append("تم اختصار بيانات Excel إلى الحد الآمن للتحليل.")
                break
    finally:
        workbook.close()
    return _clean_text("\n".join(parts), max_chars), warnings, {}


def _extract_pptx(payload: bytes, max_chars: int) -> tuple[str, list[str], dict]:
    _preflight_zip(payload)
    try:
        with zipfile.ZipFile(BytesIO(payload)) as package:
            slide_names = sorted(
                (
                    name
                    for name in package.namelist()
                    if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
                ),
                key=lambda name: int(re.search(r"(\d+)\.xml$", name).group(1)),
            )
            parts: list[str] = []
            for slide_name in slide_names[:100]:
                xml_text = package.read(slide_name).decode("utf-8", errors="replace")
                slide_parts = [
                    html.unescape(re.sub(r"<[^>]+>", "", match)).strip()
                    for match in re.findall(r"<a:t(?:\s[^>]*)?>.*?</a:t>", xml_text, re.DOTALL)
                ]
                slide_parts = [part for part in slide_parts if part]
                if slide_parts:
                    parts.append(" | ".join(slide_parts))
                if sum(len(item) + 1 for item in parts) >= max_chars:
                    break
    except (OSError, zipfile.BadZipFile) as exc:
        raise CorrespondenceIntakeError(
            "تعذر قراءة ملف PowerPoint.", code="INVALID_PPTX", status_code=422
        ) from exc
    return _clean_text("\n".join(parts), max_chars), [], {}


def _extract_eml(payload: bytes, max_chars: int) -> tuple[str, list[str], dict]:
    try:
        message = BytesParser(policy=policy.default).parsebytes(payload)
    except Exception as exc:
        raise CorrespondenceIntakeError(
            "تعذر قراءة ملف البريد الإلكتروني.", code="INVALID_EML", status_code=422
        ) from exc

    parts: list[str] = []
    for part in message.walk() if message.is_multipart() else [message]:
        if part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            content = part.get_content()
        except Exception:
            raw = part.get_payload(decode=True) or b""
            content = _decode_text(raw)
        if content_type == "text/html":
            try:
                from bs4 import BeautifulSoup

                content = BeautifulSoup(str(content), "html.parser").get_text("\n")
            except Exception:
                content = re.sub(r"<[^>]+>", " ", str(content))
        parts.append(str(content))
        if sum(len(item) + 1 for item in parts) >= max_chars:
            break

    sender_name, sender_address = parseaddr(str(message.get("From") or ""))
    metadata = {
        "subject": str(message.get("Subject") or "").strip(),
        "sender": (sender_name or sender_address or "").strip(),
        "message_date": str(message.get("Date") or "").strip(),
    }
    return _clean_text("\n".join(parts), max_chars), [], metadata


def extract_attachment_text(
    payload: bytes,
    filename: str,
    *,
    max_chars: int = 20_000,
    max_pdf_pages: int = 40,
    ocr_config: OcrConfig | None = None,
) -> dict:
    """Extract bounded text and metadata from a supported attachment."""
    safe_name = _safe_filename(filename)
    extension = Path(safe_name).suffix.lower()
    warnings: list[str] = []
    metadata: dict = {}

    if extension == ".pdf":
        text, warnings, metadata = _extract_pdf(
            payload,
            max_chars,
            max_pdf_pages,
            ocr_config,
        )
        format_label = "PDF"
    elif extension == ".docx":
        text, warnings, metadata = _extract_docx(payload, max_chars)
        format_label = "Word"
    elif extension == ".xlsx":
        text, warnings, metadata = _extract_xlsx(payload, max_chars)
        format_label = "Excel"
    elif extension == ".pptx":
        text, warnings, metadata = _extract_pptx(payload, max_chars)
        format_label = "PowerPoint"
    elif extension == ".eml":
        text, warnings, metadata = _extract_eml(payload, max_chars)
        format_label = "Email"
    elif extension in {".txt", ".csv", ".json", ".xml", ".html", ".htm"}:
        decoded = _decode_text(payload)
        if extension in {".html", ".htm"}:
            try:
                from bs4 import BeautifulSoup

                decoded = BeautifulSoup(decoded, "html.parser").get_text("\n")
            except Exception:
                decoded = re.sub(r"<[^>]+>", " ", decoded)
        text = _clean_text(decoded, max_chars)
        format_label = "Text"
    elif extension in IMAGE_EXTENSIONS:
        text, warnings, metadata = _extract_image_with_ocr(
            payload,
            max_chars,
            ocr_config,
        )
        format_label = "Image"
    else:
        raise CorrespondenceIntakeError(
            "صيغة المرفق غير مدعومة للتحليل الذكي حالياً. يمكن رفعها وحفظها بالطريقة العادية.",
            code="UNSUPPORTED_FILE_TYPE",
            status_code=415,
        )

    return {
        "filename": safe_name,
        "extension": extension,
        "format": format_label,
        "text": text,
        "metadata": metadata,
        "warnings": warnings,
        "ocr": {
            "enabled": bool(metadata.get("ocr_enabled", False)),
            "available": bool(metadata.get("ocr_available", False)),
            "used": bool(metadata.get("ocr_used", False)),
            "pages": int(metadata.get("ocr_pages", 0) or 0),
        },
    }


def _suggestion(value, confidence: float, reason: str, **extra) -> dict | None:
    if value in (None, ""):
        return None
    result = {
        "value": value,
        "confidence": round(max(0.0, min(1.0, float(confidence))), 2),
        "reason": reason,
    }
    result.update(extra)
    return result


def _choice_label(choice: dict) -> str:
    return str(choice.get("match_label") or choice.get("label") or choice.get("value") or "").strip()


def _match_choice(text: str, choices: list[dict]) -> tuple[dict | None, float]:
    normalized_text = _normalize(text)
    normalized_head = _normalize(text[:2500])
    best_choice = None
    best_score = 0.0
    best_length = 0
    for choice in choices:
        label = _choice_label(choice)
        normalized_label = _normalize(re.sub(r"\s*\([^)]*\)\s*$", "", label))
        if len(normalized_label) < 3:
            continue
        score = 0.0
        if normalized_label in normalized_head:
            score = 0.96
        elif normalized_label in normalized_text:
            score = 0.9
        else:
            tokens = {
                token
                for token in normalized_label.split()
                if len(token) >= 3 and token not in _MEANINGLESS_TOKENS
            }
            if len(tokens) >= 2:
                overlap = sum(1 for token in tokens if token in normalized_text)
                if overlap == len(tokens):
                    score = 0.78
                elif overlap / len(tokens) >= 0.75:
                    score = 0.68
        if score > best_score or (
            score == best_score and score > 0 and len(normalized_label) > best_length
        ):
            best_choice = choice
            best_score = score
            best_length = len(normalized_label)
    return best_choice, best_score


def _match_known_value(value: str, choices: list[dict]) -> dict | None:
    normalized_value = _normalize(value)
    if not normalized_value:
        return None
    best = None
    best_length = 0
    for choice in choices:
        label = _choice_label(choice)
        normalized_label = _normalize(label)
        if not normalized_label:
            continue
        if normalized_label == normalized_value or (
            len(normalized_label) >= 4
            and (normalized_label in normalized_value or normalized_value in normalized_label)
        ):
            if len(normalized_label) > best_length:
                best = choice
                best_length = len(normalized_label)
    return best


def _explicit_line_value(text: str, labels: tuple[str, ...], max_length: int = 250) -> str:
    label_pattern = "|".join(re.escape(label) for label in labels)
    pattern = re.compile(
        rf"^(?:{label_pattern})[ \t]*[:：\-][ \t]*(.+)$",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(text[:8000])
    if not match:
        return ""
    value = re.sub(r"\s+", " ", match.group(1)).strip(" -:：|\t")
    return value[:max_length]


def _suggest_subject(text: str, filename: str, metadata: dict) -> dict:
    metadata_subject = str(metadata.get("subject") or "").strip()
    if metadata_subject:
        return _suggestion(metadata_subject[:500], 0.99, "عنوان البريد أو بيانات المستند")

    explicit = _explicit_line_value(text, ("الموضوع", "الموضوع /", "بخصوص", "Subject"), 500)
    if explicit:
        return _suggestion(explicit, 0.97, "حقل الموضوع داخل المستند")

    stem = Path(filename).stem
    stem = re.sub(r"[_\-]+", " ", stem).strip()
    if stem:
        return _suggestion(stem[:500], 0.45, "اسم المرفق؛ يحتاج إلى مراجعة")
    return _suggestion("وارد جديد", 0.2, "قيمة افتراضية؛ تحتاج إلى مراجعة")


def _suggest_sender(text: str, metadata: dict, sender_choices: list[dict]) -> dict | None:
    explicit = str(metadata.get("sender") or "").strip() or _explicit_line_value(
        text,
        ("الجهة المرسلة", "المرسل", "صادر عن", "من", "From"),
        200,
    )
    if explicit:
        known = _match_known_value(explicit, sender_choices)
        if known:
            return _suggestion(
                _choice_label(known),
                0.98,
                "جهة مرسلة مطابقة لدليل الجهات",
                select_value=str(known.get("value") or _choice_label(known)),
                is_known=True,
            )
        return _suggestion(
            explicit,
            0.82,
            "جهة مرسلة مذكورة في المستند وتحتاج إلى مراجعة",
            select_value="__OTHER__",
            is_known=False,
        )

    known, score = _match_choice(text[:6000], sender_choices)
    if known:
        return _suggestion(
            _choice_label(known),
            score,
            "مطابقة اسم جهة من دليل المرسلين",
            select_value=str(known.get("value") or _choice_label(known)),
            is_known=True,
        )
    return None


def _suggest_category(text: str, category_choices: list[dict]) -> dict | None:
    choice, score = _match_choice(text, category_choices)
    if choice:
        return _suggestion(
            _choice_label(choice),
            score,
            "مطابقة تصنيف مسجل مع محتوى المستند",
            select_value=str(choice.get("value") or ""),
        )

    normalized_text = _normalize(text)
    aliases = {
        "FIN": ("مالي", "ماليه", "موازنه", "فاتوره", "دفع"),
        "HR": ("موارد بشريه", "موظف", "توظيف", "اجازه", "راتب"),
        "LEGAL": ("قانوني", "قانونيه", "عقد", "اتفاقيه", "مذكره تفاهم"),
        "PROCUREMENT": ("عطاء", "مشتريات", "توريد", "مورد"),
    }
    for code, keywords in aliases.items():
        if not any(keyword in normalized_text for keyword in keywords):
            continue
        for candidate in category_choices:
            candidate_code = str(candidate.get("value") or "").upper()
            if candidate_code == code:
                return _suggestion(
                    _choice_label(candidate),
                    0.76,
                    "تصنيف مقترح من كلمات المستند",
                    select_value=candidate_code,
                )

    for candidate in category_choices:
        if str(candidate.get("value") or "").upper() == "GENERAL":
            return _suggestion(
                _choice_label(candidate),
                0.35,
                "التصنيف العام الافتراضي؛ يحتاج إلى مراجعة",
                select_value="GENERAL",
            )
    return None


def _suggest_due_date(text: str) -> dict | None:
    patterns = (
        r"(?:الموعد النهائي|الرد قبل|قبل تاريخ|بحد اقصى|بحد أقصى)[ \t]*[:：\-]?[ \t]*(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
        r"(?:الموعد النهائي|الرد قبل|قبل تاريخ|بحد اقصى|بحد أقصى)[ \t]*[:：\-]?[ \t]*(\d{1,2}[-/]\d{1,2}[-/]\d{4})",
    )
    for pattern in patterns:
        match = re.search(pattern, text[:12000], flags=re.IGNORECASE)
        if not match:
            continue
        raw_value = match.group(1).replace("/", "-")
        for date_format in ("%Y-%m-%d", "%d-%m-%Y"):
            try:
                parsed = datetime.strptime(raw_value, date_format).date()
                return _suggestion(parsed.isoformat(), 0.92, "موعد نهائي مذكور في المستند")
            except ValueError:
                continue
    return None


def _suggest_reference(text: str) -> dict | None:
    pattern = re.compile(
        r"(?:رقم الكتاب|رقم المرجع|الرقم المرجعي|رقم الصادر|Reference(?: No\.?)?|Ref(?: No\.?)?)[ \t]*[:：#\-][ \t]*([A-Za-z0-9\u0600-\u06FF/_\-.]{2,80})",
        flags=re.IGNORECASE,
    )
    match = pattern.search(text[:8000])
    if not match:
        return None
    return _suggestion(match.group(1).strip(" .-"), 0.94, "رقم مرجع المستند")


def _suggest_priority(text: str) -> dict:
    normalized_text = _normalize(text[:12000])
    if any(token in normalized_text for token in ("عاجل جدا", "عاجل", "فوري", "علي وجه السرعه")):
        return _suggestion("URGENT", 0.93, "وجود عبارة تدل على الاستعجال")
    if any(token in normalized_text for token in ("هام", "مهم", "اولويه عاليه")):
        return _suggestion("HIGH", 0.82, "وجود عبارة تدل على الأهمية")
    return _suggestion("NORMAL", 0.65, "لم تظهر علامة واضحة على الاستعجال")


def _suggest_confidentiality(text: str) -> dict:
    normalized_text = _normalize(text[:12000])
    normalized_text = normalized_text.replace("غير سري", "")
    if any(token in normalized_text for token in ("سري للغايه", "سري", "خاص وسري")):
        return _suggestion("SECRET", 0.95, "وجود علامة سرية في المستند")
    return _suggestion("NORMAL", 0.65, "لم تظهر علامة سرية واضحة")


def _suggest_workflow(text: str, subject: str, workflow_choices: list[dict]) -> dict | None:
    combined = f"{subject}\n{text}"
    choice, score = _match_choice(combined, workflow_choices)
    if choice:
        return _suggestion(
            _choice_label(choice),
            score,
            "مطابقة اسم قالب المسار مع موضوع المستند",
            select_value=str(choice.get("value") or ""),
        )
    if len(workflow_choices) == 1:
        choice = workflow_choices[0]
        return _suggestion(
            _choice_label(choice),
            0.55,
            "قالب المسار النشط الوحيد؛ يحتاج إلى مراجعة",
            select_value=str(choice.get("value") or ""),
        )
    return None


def analyze_correspondence_attachment(
    payload: bytes,
    filename: str,
    *,
    sender_choices: list[dict] | None = None,
    category_choices: list[dict] | None = None,
    competence_choices: list[dict] | None = None,
    workflow_choices: list[dict] | None = None,
    received_date: str | None = None,
    max_text_chars: int = 20_000,
    max_pdf_pages: int = 40,
    ocr_config: OcrConfig | None = None,
) -> dict:
    """Extract an attachment and return reviewable inbound-field suggestions."""
    extracted = extract_attachment_text(
        payload,
        filename,
        max_chars=max_text_chars,
        max_pdf_pages=max_pdf_pages,
        ocr_config=ocr_config,
    )
    text = extracted["text"]
    metadata = extracted["metadata"]
    sender_choices = list(sender_choices or [])
    category_choices = list(category_choices or [])
    competence_choices = list(competence_choices or [])
    workflow_choices = list(workflow_choices or [])

    subject = _suggest_subject(text, extracted["filename"], metadata)
    sender = _suggest_sender(text, metadata, sender_choices)
    category = _suggest_category(text, category_choices)

    competence_choice, competence_score = _match_choice(text, competence_choices)
    competence = None
    if competence_choice:
        competence = _suggestion(
            _choice_label(competence_choice),
            competence_score,
            "مطابقة جهة اختصاص من الهيكل التنظيمي",
            select_value=str(competence_choice.get("value") or ""),
        )

    workflow = _suggest_workflow(text, str(subject.get("value") or ""), workflow_choices)
    suggestions = {
        "received_date": _suggestion(
            received_date or date.today().isoformat(),
            1.0,
            "تاريخ الاستلام الحالي؛ عدّله عند الحاجة",
        ),
        "subject": subject,
        "sender": sender,
        "category": category,
        "competence": competence,
        "workflow_template": workflow,
        "priority": _suggest_priority(text),
        "confidentiality": _suggest_confidentiality(text),
        "due_date": _suggest_due_date(text),
        "document_reference": _suggest_reference(text),
        "mail_scope": _suggestion("EXTERNAL", 0.75, "المرفق مسجل كبريد وارد خارجي"),
        "body": _suggestion(text[:8000], 0.88, "النص المستخرج محلياً من المرفق") if text else None,
    }

    return {
        "filename": extracted["filename"],
        "format": extracted["format"],
        "extracted_characters": len(text),
        "preview": text[:1200],
        "warnings": extracted["warnings"],
        "ocr": extracted["ocr"],
        "suggestions": {key: value for key, value in suggestions.items() if value},
        "privacy": "LOCAL_ONLY",
    }
