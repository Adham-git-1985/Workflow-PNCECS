from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps, features

from utils.corr_refs import correspondence_reference_label


STAMPABLE_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
STAMPABLE_EXTS = STAMPABLE_IMAGE_EXTS | {".pdf"}


@dataclass(frozen=True)
class CorrStampOptions:
    enabled: bool
    kind: str
    ref_no: str
    stamp_date: str


def is_stampable_file(filename: str) -> bool:
    return Path(filename or "").suffix.lower() in STAMPABLE_EXTS


def apply_corr_stamp(file_path: str, options: CorrStampOptions) -> bool:
    if not options.enabled or not is_stampable_file(file_path):
        return False

    ext = Path(file_path).suffix.lower()
    if ext == ".pdf":
        return _stamp_pdf(file_path, options)
    if ext in STAMPABLE_IMAGE_EXTS:
        return _stamp_image(file_path, options)
    return False


def _stamp_pdf(file_path: str, options: CorrStampOptions) -> bool:
    try:
        import fitz
    except Exception:
        return False

    stamp = build_stamp_image(options)
    tmp_png = None
    tmp_pdf = None
    doc = None
    try:
        fd, tmp_png = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        stamp.save(tmp_png, "PNG")

        doc = fitz.open(file_path)
        if doc.page_count < 1:
            return False

        page = doc[0]
        rect = page.rect
        stamp_width = min(max(rect.width * 0.32, 150), 230)
        stamp_height = stamp_width * stamp.height / stamp.width
        margin = max(22, rect.width * 0.035)
        box = fitz.Rect(
            rect.x1 - margin - stamp_width,
            rect.y0 + margin,
            rect.x1 - margin,
            rect.y0 + margin + stamp_height,
        )
        page.insert_image(box, filename=tmp_png, overlay=True)

        fd, tmp_pdf = tempfile.mkstemp(suffix=".pdf", dir=str(Path(file_path).parent))
        os.close(fd)
        doc.save(tmp_pdf, garbage=4, deflate=True)
        doc.close()
        doc = None
        os.replace(tmp_pdf, file_path)
        return True
    except Exception:
        return False
    finally:
        try:
            if doc is not None:
                doc.close()
        except Exception:
            pass
        for path in (tmp_png, tmp_pdf):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass


def _stamp_image(file_path: str, options: CorrStampOptions) -> bool:
    try:
        with Image.open(file_path) as src:
            src_format = src.format
            base = ImageOps.exif_transpose(src).convert("RGBA")

        stamp = build_stamp_image(options)
        width = int(min(max(base.width * 0.38, 240), 700))
        if base.width < 520:
            width = int(base.width * 0.72)
        height = int(width * stamp.height / stamp.width)
        stamp = stamp.resize((width, height), Image.Resampling.LANCZOS)

        margin = max(16, int(base.width * 0.04))
        x = max(0, base.width - width - margin)
        y = margin
        base.alpha_composite(stamp, (x, y))

        ext = Path(file_path).suffix.lower()
        if ext in {".jpg", ".jpeg"}:
            base.convert("RGB").save(file_path, format="JPEG", quality=95)
        else:
            save_format = src_format or _format_from_ext(ext)
            base.save(file_path, format=save_format)
        return True
    except Exception:
        return False


def build_stamp_image(options: CorrStampOptions) -> Image.Image:
    blue = (20, 38, 190, 255)
    canvas = Image.new("RGBA", (720, 300), (255, 255, 255, 0))
    draw = ImageDraw.Draw(canvas)

    draw.rounded_rectangle((8, 8, 712, 292), radius=8, fill=(255, 255, 255, 235), outline=blue, width=8)

    eagle = _load_eagle()
    if eagle is not None:
        eagle = eagle.resize((126, 150), Image.Resampling.LANCZOS)
        canvas.alpha_composite(eagle, (555, 45))

    regular = _font(36, bold=True)
    bold = _font(42, bold=True)
    date_font = _font(38, bold=True)

    text_right = 530
    _draw_centered(draw, "اللجنة الوطنية الفلسطينية", regular, blue, 38, text_right, 42, stroke_width=1)
    _draw_centered(draw, "للتربية والثقافة والعلوم", regular, blue, 38, text_right, 88, stroke_width=1)
    _draw_centered(draw, _format_stamp_date(options.stamp_date), date_font, blue, 38, text_right, 155, stroke_width=1)

    label = correspondence_reference_label(
        options.kind,
        options.ref_no,
        include_number_word=(options.kind or "").upper() == "OUT",
    )
    _draw_centered(draw, label, bold, blue, 38, text_right, 212, stroke_width=1)

    return canvas


def _draw_centered(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    fill,
    x1: int,
    x2: int,
    y: int,
    stroke_width: int = 0,
) -> None:
    render_text = text
    text_options = {"font": font, "stroke_width": stroke_width}
    if _contains_arabic(text) and features.check("raqm"):
        text_options["direction"] = "rtl"
    else:
        render_text = _shape_stamp_text(text)

    bbox = draw.textbbox((0, 0), render_text, **text_options)
    width = bbox[2] - bbox[0]
    x = x1 + ((x2 - x1 - width) / 2)
    draw.text((x, y), render_text, fill=fill, stroke_fill=fill, **text_options)


def _contains_arabic(text: str) -> bool:
    return any("\u0600" <= char <= "\u08ff" for char in text)


def _shape_stamp_text(text: str) -> str:
    first_digit = next((index for index, char in enumerate(text) if char.isascii() and char.isdigit()), None)
    if first_digit is None:
        return _shape_arabic(text)

    boundary = first_digit
    while boundary and text[boundary - 1] in " -/":
        boundary -= 1
    prefix = text[:boundary]
    separator = text[boundary:first_digit]
    if not prefix or not _contains_arabic(prefix) or any(
        char.isascii() and char.isalpha() for char in prefix
    ):
        return _shape_arabic(text)

    return f"{text[first_digit:]}{separator}{_shape_arabic(prefix)}"


def _shape_arabic(text: str) -> str:
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        return get_display(arabic_reshaper.reshape(text or ""))
    except Exception:
        return _shape_arabic_fallback(text or "")


_ARABIC_FORMS = {
    "آ": ("ﺁ", "ﺂ", None, None),
    "أ": ("ﺃ", "ﺄ", None, None),
    "ؤ": ("ﺅ", "ﺆ", None, None),
    "إ": ("ﺇ", "ﺈ", None, None),
    "ئ": ("ﺉ", "ﺊ", "ﺋ", "ﺌ"),
    "ا": ("ﺍ", "ﺎ", None, None),
    "ب": ("ﺏ", "ﺐ", "ﺑ", "ﺒ"),
    "ة": ("ﺓ", "ﺔ", None, None),
    "ت": ("ﺕ", "ﺖ", "ﺗ", "ﺘ"),
    "ث": ("ﺙ", "ﺚ", "ﺛ", "ﺜ"),
    "ج": ("ﺝ", "ﺞ", "ﺟ", "ﺠ"),
    "ح": ("ﺡ", "ﺢ", "ﺣ", "ﺤ"),
    "خ": ("ﺥ", "ﺦ", "ﺧ", "ﺨ"),
    "د": ("ﺩ", "ﺪ", None, None),
    "ذ": ("ﺫ", "ﺬ", None, None),
    "ر": ("ﺭ", "ﺮ", None, None),
    "ز": ("ﺯ", "ﺰ", None, None),
    "س": ("ﺱ", "ﺲ", "ﺳ", "ﺴ"),
    "ش": ("ﺵ", "ﺶ", "ﺷ", "ﺸ"),
    "ص": ("ﺹ", "ﺺ", "ﺻ", "ﺼ"),
    "ض": ("ﺽ", "ﺾ", "ﺿ", "ﻀ"),
    "ط": ("ﻁ", "ﻂ", "ﻃ", "ﻄ"),
    "ظ": ("ﻅ", "ﻆ", "ﻇ", "ﻈ"),
    "ع": ("ﻉ", "ﻊ", "ﻋ", "ﻌ"),
    "غ": ("ﻍ", "ﻎ", "ﻏ", "ﻐ"),
    "ف": ("ﻑ", "ﻒ", "ﻓ", "ﻔ"),
    "ق": ("ﻕ", "ﻖ", "ﻗ", "ﻘ"),
    "ك": ("ﻙ", "ﻚ", "ﻛ", "ﻜ"),
    "ل": ("ﻝ", "ﻞ", "ﻟ", "ﻠ"),
    "م": ("ﻡ", "ﻢ", "ﻣ", "ﻤ"),
    "ن": ("ﻥ", "ﻦ", "ﻧ", "ﻨ"),
    "ه": ("ﻩ", "ﻪ", "ﻫ", "ﻬ"),
    "و": ("ﻭ", "ﻮ", None, None),
    "ى": ("ﻯ", "ﻰ", None, None),
    "ي": ("ﻱ", "ﻲ", "ﻳ", "ﻴ"),
}


def _shape_arabic_fallback(text: str) -> str:
    if not any(ch in _ARABIC_FORMS for ch in text):
        return text
    tokens = text.split(" ")
    shaped_tokens = []
    for token in tokens:
        if any(ch in _ARABIC_FORMS for ch in token):
            shaped_tokens.append(_shape_arabic_token(token)[::-1])
        else:
            shaped_tokens.append(token)
    return " ".join(reversed(shaped_tokens))


def _shape_arabic_token(token: str) -> str:
    chars = list(token)
    out = []
    for idx, ch in enumerate(chars):
        forms = _ARABIC_FORMS.get(ch)
        if not forms:
            out.append(ch)
            continue

        prev_ch = _previous_arabic(chars, idx)
        next_ch = _next_arabic(chars, idx)
        connects_prev = bool(prev_ch and _can_connect_next(prev_ch) and _can_connect_prev(ch))
        connects_next = bool(next_ch and _can_connect_next(ch) and _can_connect_prev(next_ch))

        isolated, final, initial, medial = forms
        if connects_prev and connects_next and medial:
            out.append(medial)
        elif connects_prev and final:
            out.append(final)
        elif connects_next and initial:
            out.append(initial)
        else:
            out.append(isolated)
    return "".join(out)


def _previous_arabic(chars: list[str], idx: int) -> str | None:
    for pos in range(idx - 1, -1, -1):
        if chars[pos] in _ARABIC_FORMS:
            return chars[pos]
        if chars[pos].isalnum():
            return None
    return None


def _next_arabic(chars: list[str], idx: int) -> str | None:
    for pos in range(idx + 1, len(chars)):
        if chars[pos] in _ARABIC_FORMS:
            return chars[pos]
        if chars[pos].isalnum():
            return None
    return None


def _can_connect_prev(ch: str) -> bool:
    return ch in _ARABIC_FORMS


def _can_connect_next(ch: str) -> bool:
    forms = _ARABIC_FORMS.get(ch)
    return bool(forms and forms[2])


def _format_stamp_date(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.strftime("%d - %m - %Y")
        except ValueError:
            pass
    return value


def _load_eagle() -> Image.Image | None:
    path = Path(__file__).resolve().parents[1] / "static" / "images" / "corr_stamp_eagle.png"
    if not path.exists():
        return None
    try:
        img = Image.open(path).convert("RGBA")
        pixels = img.load()
        for y in range(img.height):
            for x in range(img.width):
                r, g, b, a = pixels[x, y]
                if a and r > 245 and g > 245 and b > 245:
                    pixels[x, y] = (255, 255, 255, 0)
        return img
    except Exception:
        return None


@lru_cache(maxsize=16)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\tahomabd.ttf" if bold else r"C:\Windows\Fonts\tahoma.ttf",
        r"C:\Windows\Fonts\seguisb.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if path and Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def _format_from_ext(ext: str) -> str | None:
    return {
        ".png": "PNG",
        ".bmp": "BMP",
        ".tif": "TIFF",
        ".tiff": "TIFF",
        ".webp": "WEBP",
    }.get(ext.lower())
