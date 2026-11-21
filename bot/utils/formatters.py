"""Text formatting helpers for bot messages and API payloads.

Everything here is pure (no IO, no state) which makes the UI layer trivially
testable: sizes, durations, dates, progress bars, HTML escaping and pagination
arithmetic all live in one module.
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final

__all__ = [
    "bullet_list",
    "clamp_page",
    "escape_html",
    "escape_markdown_v2",
    "format_date",
    "format_duration",
    "format_number",
    "format_relative_time",
    "format_size",
    "human_size_pair",
    "key_value_line",
    "mask_token",
    "pages_count",
    "parse_size_input",
    "pluralize",
    "progress_bar",
    "storage_status_emoji",
    "truncate",
    "truncate_middle",
]

_SIZE_UNITS: Final[tuple[tuple[str, float], ...]] = (
    ("TB", 1024**4),
    ("GB", 1024**3),
    ("MB", 1024**2),
    ("KB", 1024),
    ("B", 1),
)

_MD2_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")
_HTML_UNSAFE = re.compile(r"<[^>]+>")


def format_size(size: float | None, *, precision: int = 1, binary: bool = True) -> str:
    """Human readable byte size: ``1536`` -> ``"1.5 KB"``.

    ``binary=False`` uses SI units (1000-based) which some users expect on the
    web side.
    """
    if size is None:
        return "0 B"
    value = float(size)
    if value < 0:
        return f"-{format_size(-value, precision=precision, binary=binary)}"
    step = 1024 if binary else 1000
    units = ("B", "KB", "MB", "GB", "TB", "PB") if binary else ("B", "kB", "MB", "GB", "TB", "PB")

    for unit in units:
        if abs(value) < step or unit == units[-1]:
            if unit == "B" or precision <= 0:
                return f"{round(value)} {unit}"
            # Trim trailing zeros but keep at least one decimal digit:
            # 1536 -> "1.5 KB", 10 GiB -> "10.0 GB", 1.25 GiB -> "1.25 GB".
            formatted = f"{value:.{precision}f}".rstrip("0")
            if formatted.endswith("."):
                formatted += "0"
            return f"{formatted} {unit}"
        value /= step
    return f"{value:.{precision}f} PB"  # pragma: no cover


def format_number(value: float) -> str:
    """Thousands separated integer, e.g. ``1234567`` -> ``"1 234 567"``."""
    return f"{int(value):,}".replace(",", " ")


def format_duration(seconds: float | None) -> str:
    """``3725`` -> ``"1:02:05"``, ``65`` -> ``"1:05"`` (media-player style)."""
    if not seconds or seconds < 0:
        return "0:00"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_date(value: datetime | None, *, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Format a datetime in UTC (naive values are assumed to be UTC)."""
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime(fmt)


def format_relative_time(value: datetime | None, *, now: datetime | None = None) -> str:
    """Short human delta: ``"just now"``, ``"5 min ago"``, ``"3 days ago"``."""
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    moment = now or datetime.now(UTC)
    delta: timedelta = moment - value
    seconds = int(delta.total_seconds())

    if seconds < 0:
        return "soon"
    if seconds < 45:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return pluralize(minutes, "minute") + " ago"
    hours = minutes // 60
    if hours < 24:
        return pluralize(hours, "hour") + " ago"
    days = hours // 24
    if days < 30:
        return pluralize(days, "day") + " ago"
    months = days // 30
    if months < 12:
        return pluralize(months, "month") + " ago"
    return pluralize(days // 365, "year") + " ago"


def pluralize(count: int, word: str, *, plural: str | None = None) -> str:
    """English pluralisation: ``pluralize(2, "file")`` -> ``"2 files"``."""
    suffix = plural if plural is not None else f"{word}s"
    return f"{format_number(count)} {word if count == 1 else suffix}"


def progress_bar(
    value: float,
    total: float,
    *,
    width: int = 10,
    filled: str = "█",
    empty: str = "░",
    show_percent: bool = True,
) -> str:
    """Textual progress bar used for the storage quota widget.

    >>> progress_bar(3, 4, width=4)
    '███░ 75%'
    """
    if total <= 0:
        ratio = 0.0
    else:
        ratio = max(0.0, min(1.0, value / total))
    blocks = round(ratio * width)
    bar = filled * blocks + empty * (width - blocks)
    return f"{bar} {int(ratio * 100)}%" if show_percent else bar


def storage_status_emoji(used: int, quota: int) -> str:
    """Traffic-light indicator for the quota bar."""
    if quota <= 0:
        return "🟩"
    ratio = used / quota
    if ratio >= 0.95:
        return "🟥"
    if ratio >= 0.75:
        return "🟨"
    return "🟩"


def escape_html(text: Any, *, strip_tags: bool = False) -> str:
    """Escape text for ``parse_mode=HTML`` messages.

    ``strip_tags=True`` additionally removes any tag-like sequences *before*
    escaping, which is handy for user captions that already contain markup.
    """
    raw = "" if text is None else str(text)
    if strip_tags:
        raw = _HTML_UNSAFE.sub("", raw)
    return html.escape(raw, quote=False)


def escape_markdown_v2(text: Any) -> str:
    """Escape text for ``parse_mode=MarkdownV2``."""
    return _MD2_SPECIAL.sub(r"\\\1", "" if text is None else str(text))


def truncate(text: str, limit: int = 60, *, ellipsis: str = "…") -> str:
    """Cut a long file name so keyboards stay readable."""
    if not text or len(text) <= limit:
        return text or ""
    keep = max(1, limit - len(ellipsis))
    return text[:keep] + ellipsis


def truncate_middle(text: str, limit: int = 40, *, ellipsis: str = "…") -> str:
    """Keep both the start and the extension: ``very…name.pdf``."""
    if not text or len(text) <= limit:
        return text or ""
    if "." in text:
        stem, _, extension = text.rpartition(".")
        extension = f".{extension}"
    else:
        stem, extension = text, ""
    available = max(1, limit - len(ellipsis) - len(extension))
    return f"{stem[:available]}{ellipsis}{extension}"


def pages_count(total: int, page_size: int) -> int:
    """How many pages ``total`` items occupy (at least one)."""
    if page_size <= 0:
        return 1
    return max(1, -(-total // page_size))


def clamp_page(page: int, total: int, page_size: int) -> int:
    """Keep a requested page inside ``[1, pages_count]``."""
    return max(1, min(page, pages_count(total, page_size)))


def mask_token(token: str, *, visible: int = 4) -> str:
    """Show only the edges of a token in logs/messages: ``Abcd…wxyz``."""
    if not token:
        return ""
    if len(token) <= visible * 2:
        return token
    return f"{token[:visible]}…{token[-visible:]}"


def bullet_list(items: Sequence[str], *, marker: str = "•") -> str:
    """Render a newline separated bullet list (empty input -> empty string)."""
    return "\n".join(f"{marker} {item}" for item in items if item)


def key_value_line(key: str, value: Any, *, separator: str = ": ") -> str:
    """``"Size: 1.5 MB"`` style line with HTML-escaped parts."""
    return f"<b>{escape_html(key)}</b>{separator}{escape_html(value)}"


def human_size_pair(used: int, quota: int) -> str:
    """``"1.5 GB / 5 GB"`` for the storage header."""
    if quota <= 0:
        return f"{format_size(used)} / unlimited"
    return f"{format_size(used)} / {format_size(quota)}"


def parse_size_input(text: str) -> int | None:
    """Parse a user-typed size such as ``"500MB"`` into bytes."""
    cleaned = text.strip().upper().replace(" ", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(B|KB|MB|GB|TB)?", cleaned)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2) or "B"
    multiplier = _SIZE_UNITS.get(unit, 1)
    return int(number * multiplier)
