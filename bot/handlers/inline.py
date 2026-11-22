"""Inline mode: search your cloud from **any** chat.

Typing ``@your_cloud_bot invoice`` in a group, a channel comment section or
somebody else's DM returns the caller's own files as inline results.

Group prefixes (``hugecloud``)
-----------------------------
The query may start with a group key so users can narrow the search without
opening the bot::

    @Huge_cloud_bot video hisob-faktura   -> 🎬 media group only
    @Huge_cloud_bot audio podcast         -> 🎵 audio group only
    @Huge_cloud_bot doc shartnoma         -> 📄 documents only
    @Huge_cloud_bot arxiv zip             -> 📦 archives only
    @Huge_cloud_bot apk                   -> 🤖 APK/IPA only

Recognised keys: ``media``/``video``/``rasm``/``photo``, ``audio``/``musiqa``,
``doc``/``document``/``hujjat``, ``arxiv``/``archive``/``zip``, ``apps``/``apk``,
``pinned``/``muhim``.

Privacy rules enforced here:

* the caller must be **authenticated** (``users.is_authenticated``) - inline mode
  is a cloud feature, so the auth guard applies here too;
* ``is_personal=True`` — results are only displayed to the user who typed the
  query; nobody else in the chat can page through them.
* the account-level ``inline_enabled`` preference can disable the feature;
* queries are throttled by ``INLINE_RATE_LIMIT``/``INLINE_RATE_WINDOW`` and the
  DB search is cached for ``CACHE_TTL_SEARCH`` seconds (every keystroke is a
  query, so this matters).

Results use the **cached** result types (``InlineQueryResultCached*``): Telegram
already holds the media by ``file_id``, so picking a result sends it instantly
without any traffic through this server.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import (
    InlineQuery,
    InlineQueryResult,
    InlineQueryResultCachedAudio,
    InlineQueryResultCachedDocument,
    InlineQueryResultCachedMpeg4Gif,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    InlineQueryResultCachedVoice,
)

from bot.utils.emojis import Emoji
from bot.utils.formatters import (
    escape_html,
    format_duration,
    format_size,
    truncate_middle,
)
from config import Settings
from db.models import File, FileCategory, User
from services.storage import StorageService

logger = logging.getLogger(__name__)

router = Router(name="inline")

#: Minimum query length before hitting the database ("" lists recent files).
MIN_QUERY_LENGTH = 0

#: Inline so'rov boshidagi guruh prefikslari -> ``/files`` guruh kaliti.
GROUP_PREFIXES: dict[str, str] = {
    "media": "media",
    "video": "media",
    "videolar": "media",
    "rasm": "media",
    "rasmlar": "media",
    "photo": "media",
    "audio": "audio",
    "audiolar": "audio",
    "musiqa": "audio",
    "doc": "document",
    "docs": "document",
    "document": "document",
    "hujjat": "document",
    "hujjatlar": "document",
    "arxiv": "archive",
    "archive": "archive",
    "zip": "archive",
    "apps": "apps",
    "app": "apps",
    "apk": "apps",
    "ipa": "apps",
    "pinned": "pinned",
    "muhim": "pinned",
    "muhimlar": "pinned",
}


def split_group(query: str) -> tuple[str | None, str]:
    """``"video hisob"`` -> ``("media", "hisob")``; prefiks bo'lmasa ``(None, query)``."""
    cleaned = (query or "").strip()
    if not cleaned:
        return None, ""
    head, _, rest = cleaned.partition(" ")
    group = GROUP_PREFIXES.get(head.lower())
    if group is None:
        return None, cleaned
    return group, rest.strip()


@router.inline_query()
async def on_inline_query(
    inline_query: InlineQuery,
    user: User,
    storage: StorageService,
    settings: Settings,
) -> None:
    """Answer one inline query with the caller's matching files."""
    query = (inline_query.query or "").strip()
    offset = _parse_offset(inline_query.offset)
    limit = settings.storage.inline_results_limit

    # Autentifikatsiya: hisob tasdiqlanmagan bo'lsa inline qidiruv ishlamaydi.
    if settings.auth.required and not user.is_signed_in and not settings.bot.is_admin(
        user.telegram_id
    ):
        await inline_query.answer(
            [],
            cache_time=5,
            is_personal=True,
            switch_pm_text=f"{Emoji.LOCK.plain()} Avval /login qiling",
            switch_pm_parameter="auth",
        )
        return

    if not user.setting("inline_enabled", True):
        # Inline answers cannot carry plain text: answer with zero results and a
        # "start the bot" button that explains how to re-enable the feature.
        await inline_query.answer(
            [],
            cache_time=5,
            is_personal=True,
            switch_pm_text=f"{Emoji.LOCK.plain()} Inline search is off",
            switch_pm_parameter="inline_off",
        )
        return

    if len(query) < MIN_QUERY_LENGTH:  # pragma: no cover - constant guard
        await inline_query.answer([], cache_time=1, is_personal=True)
        return

    group, search_text = split_group(query)

    try:
        records = await storage.search_files(
            user.telegram_id, search_text, limit=limit + offset, group=group
        )
    except Exception:
        logger.exception("Inline search failed for %s", user.telegram_id)
        await inline_query.answer([], cache_time=1, is_personal=True)
        return

    page = records[offset : offset + limit]
    results = [_to_result(record, position) for position, record in enumerate(page, start=offset)]

    if not results:
        nothing = f"{Emoji.EMPTY.plain()} Hech narsa topilmadi"
        empty = f"{Emoji.EMPTY.plain()} Bulut bo'sh"
        await inline_query.answer(
            [],
            cache_time=settings.cache.ttl_search,
            is_personal=True,
            switch_pm_text=nothing if search_text else empty,
            switch_pm_parameter=_pm_parameter(search_text),
        )
        return

    await inline_query.answer(
        results,
        cache_time=settings.cache.ttl_search,
        is_personal=True,
        # Lets the user continue the search inside the bot chat in one tap.
        switch_pm_text=f"{Emoji.SEARCH.plain()} Open my cloud",
        switch_pm_parameter=_pm_parameter(query),
        next_offset=str(offset + limit) if len(records) > offset + limit else "",
    )


# ---------------------------------------------------------------------------
# Result builders
# ---------------------------------------------------------------------------
def _to_result(record: File, position: int) -> InlineQueryResult:
    """Map one :class:`File` row onto the matching cached inline result type."""
    result_id = f"f{record.id.hex[:16]}{position}"
    title = truncate_middle(record.file_name, 60)
    description = _description(record)
    caption = _caption(record)

    category = record.category
    if category is FileCategory.PHOTO:
        return InlineQueryResultCachedPhoto(
            id=result_id,
            photo_file_id=record.best_file_id,
            title=title,
            description=description,
            caption=caption,
            parse_mode="HTML",
        )
    if category is FileCategory.VIDEO:
        return InlineQueryResultCachedVideo(
            id=result_id,
            video_file_id=record.best_file_id,
            title=title,
            description=description,
            caption=caption,
            parse_mode="HTML",
        )
    # Animations are GIFs for the Bot API; fall back to document when the file is
    # not an MP4 GIF (Telegram rejects other containers here).
    gif_mime_types = {"video/mp4", "image/gif", ""}
    if category is FileCategory.ANIMATION and (record.mime_type or "").lower() in gif_mime_types:
        return InlineQueryResultCachedMpeg4Gif(
            id=result_id,
            mpeg4_file_id=record.best_file_id,
            title=title,
            caption=caption,
            parse_mode="HTML",
        )
    if category is FileCategory.AUDIO:
        return InlineQueryResultCachedAudio(
            id=result_id,
            audio_file_id=record.best_file_id,
            title=title,
            caption=caption,
            parse_mode="HTML",
        )
    if category is FileCategory.VOICE:
        return InlineQueryResultCachedVoice(
            id=result_id,
            voice_file_id=record.best_file_id,
            title=title,
            caption=caption,
            parse_mode="HTML",
        )
    return InlineQueryResultCachedDocument(
        id=result_id,
        document_file_id=record.best_file_id,
        title=title,
        description=description,
        caption=caption,
        parse_mode="HTML",
    )


def _pm_parameter(query: str) -> str:
    """Sanitised ``switch_pm_parameter`` (Telegram allows ``[A-Za-z0-9_-]``, ≤64).

    Prefixing with ``s_`` lets ``/start`` recognise "user came from inline mode"
    without ever echoing arbitrary text back into a deep link.
    """
    cleaned = "".join(char for char in query if char.isalnum() or char in "_-")
    return f"s_{cleaned[:60]}" if cleaned else "inline"


def _description(record: File) -> str:
    """Secondary line under the title: size (+ duration for media)."""
    parts = [format_size(record.file_size)]
    if record.duration:
        parts.append(format_duration(record.duration))
    return " · ".join(parts)


def _caption(record: File) -> str:
    """Caption attached to the sent message (HTML, escaped)."""
    icon = Emoji.for_category(record.category)
    lines = [f"{icon} <b>{escape_html(truncate_middle(record.file_name, 60))}</b>", _description(record)]
    if record.caption:
        lines.append(escape_html(truncate_middle(record.caption, 120)))
    return "\n".join(lines)


def _parse_offset(offset: str | None) -> int:
    """Inline ``offset`` is an opaque string - we use a numeric index."""
    try:
        return max(0, int(offset or 0))
    except (TypeError, ValueError):
        return 0
