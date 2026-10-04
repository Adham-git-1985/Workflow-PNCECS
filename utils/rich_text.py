"""Small, strict rich-text sanitizer for trusted system-authored content.

The editor used for administrative update broadcasts deliberately supports a
small formatting vocabulary.  Keeping the policy here (rather than trusting
the browser) lets the application render the saved markup with ``|safe``
without exposing recipients to active content.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Comment


# ``Message.body`` is a Text column, but an application-level limit protects
# the compose page and keeps a broadcast from becoming unexpectedly large.
MAX_NOTIFICATION_RICH_TEXT_CHARS = 100_000

_ALLOWED_TAGS = frozenset({
    "a",
    "b",
    "blockquote",
    "br",
    "div",
    "em",
    "font",
    "h2",
    "h3",
    "h4",
    "hr",
    "i",
    "li",
    "ol",
    "p",
    "s",
    "span",
    "strong",
    "u",
    "ul",
})
_DROP_WITH_CONTENT_TAGS = frozenset({
    "applet",
    "base",
    "embed",
    "form",
    "frame",
    "frameset",
    "iframe",
    "link",
    "meta",
    "object",
    "script",
    "style",
    "svg",
    "template",
})
_HEX_COLOR_RE = re.compile(r"^#[0-9a-f]{3}(?:[0-9a-f]{3})?$", re.IGNORECASE)
_RGB_COLOR_RE = re.compile(
    r"^rgb\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)$",
    re.IGNORECASE,
)
_RGBA_COLOR_RE = re.compile(
    r"^rgba\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*"
    r"(0(?:\.\d+)?|1(?:\.0+)?)\s*\)$",
    re.IGNORECASE,
)
_NAMED_COLORS = frozenset({
    "aqua", "black", "blue", "brown", "fuchsia", "gray", "green",
    "grey", "lime", "maroon", "navy", "olive", "orange", "pink",
    "purple", "red", "silver", "teal", "white", "yellow",
})
_TEXT_ALIGNMENTS = frozenset({"left", "right", "center", "justify"})


def _safe_color(value: str) -> str | None:
    candidate = " ".join((value or "").strip().split()).lower()
    if _HEX_COLOR_RE.fullmatch(candidate) or candidate in _NAMED_COLORS:
        return candidate
    rgb_match = _RGB_COLOR_RE.fullmatch(candidate)
    if rgb_match:
        channels = tuple(int(channel) for channel in rgb_match.groups())
        if all(0 <= channel <= 255 for channel in channels):
            return f"rgb({channels[0]}, {channels[1]}, {channels[2]})"
    rgba_match = _RGBA_COLOR_RE.fullmatch(candidate)
    if rgba_match:
        red, green, blue = (int(channel) for channel in rgba_match.groups()[:3])
        if all(0 <= channel <= 255 for channel in (red, green, blue)):
            return f"rgba({red}, {green}, {blue}, {rgba_match.group(4)})"
    return None


def _safe_style(value: str) -> str:
    """Allow only presentation attributes produced by the editor toolbar."""
    cleaned: list[str] = []
    for declaration in (value or "").split(";"):
        property_name, separator, property_value = declaration.partition(":")
        if not separator:
            continue
        property_name = property_name.strip().lower()
        normalized_value = " ".join(property_value.strip().split()).lower()
        if property_name in {"color", "background-color"}:
            color = _safe_color(normalized_value)
            if color:
                cleaned.append(f"{property_name}: {color}")
        elif property_name == "text-align" and normalized_value in _TEXT_ALIGNMENTS:
            cleaned.append(f"text-align: {normalized_value}")
    return "; ".join(cleaned)


def _safe_href(value: str) -> str | None:
    candidate = (value or "").strip()
    if (
        not candidate
        or candidate.startswith(("//", "/\\"))
        or any(ord(character) < 32 for character in candidate)
    ):
        return None
    if candidate.startswith(("/", "#")):
        return candidate
    parsed = urlparse(candidate)
    if parsed.scheme.lower() in {"http", "https", "mailto"}:
        return candidate
    return None


def sanitize_notification_rich_text(value: str | None) -> str:
    """Return safe HTML suitable for an administrative broadcast body.

    Unrecognised tags are unwrapped so ordinary text is retained.  Active and
    external-resource tags are removed together with their content.
    """
    raw_html = str(value or "").strip()
    if not raw_html:
        return ""

    soup = BeautifulSoup(raw_html, "html.parser")
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()
    for tag in soup.find_all(list(_DROP_WITH_CONTENT_TAGS)):
        tag.decompose()

    for tag in list(soup.find_all(True)):
        tag_name = (tag.name or "").lower()
        if tag_name not in _ALLOWED_TAGS:
            tag.unwrap()
            continue

        if tag_name == "font":
            # Browser ``execCommand('foreColor')`` may create <font> nodes.
            # Convert them to the same narrowly-scoped style we allow on span.
            color = _safe_color(str(tag.attrs.get("color") or ""))
            tag.name = "span"
            tag.attrs = {"style": f"color: {color}"} if color else {}
            continue

        if tag_name == "a":
            href = _safe_href(str(tag.attrs.get("href") or ""))
            if not href:
                tag.unwrap()
                continue
            attrs = {"href": href}
            if str(tag.attrs.get("target") or "").lower() == "_blank":
                attrs["target"] = "_blank"
                attrs["rel"] = "noopener noreferrer"
            title = " ".join(str(tag.attrs.get("title") or "").split())[:200]
            if title:
                attrs["title"] = title
            tag.attrs = attrs
            continue

        attrs: dict[str, str] = {}
        style = _safe_style(str(tag.attrs.get("style") or ""))
        if style:
            attrs["style"] = style
        direction = str(tag.attrs.get("dir") or "").lower()
        if direction in {"rtl", "ltr", "auto"}:
            attrs["dir"] = direction
        tag.attrs = attrs

    return str(soup).strip()
