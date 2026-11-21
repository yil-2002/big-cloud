"""Telegram-side plumbing: ``getFile``, download URLs and message payload mapping.

Telegram never gives a bot a permanent HTTP URL. The flow is:

1. ``getFile(file_id)`` -> ``file_path`` (valid for roughly one hour);
2. ``https://api.telegram.org/file/bot<TOKEN>/<file_path>`` -> the bytes.

``file_path`` results are cached in Redis (``CACHE_TTL_TG_PATH``) so repeated
playback/seeking does not hammer the Bot API, and a *local* Bot API server is
supported through ``LOCAL_BOT_API`` (it lifts the 20 MB download limit).

The module also contains the mapping helpers that turn an aiogram ``Message``
into the fields of our :class:`~db.models.file.File` row, which keeps handler
code short and testable.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from config import Settings, get_settings
from db.models import FileCategory

# Eslatma: ``File`` **runtime**da ishlatiladi (``sanitize_filename``,
# ``detect_category``) — shu sababli TYPE_CHECKING ortida yashirmang.
from db.models.file import File
from services.cache import Cache, get_cache
from services.exceptions import TelegramAPIError as TelegramAPIErrorDomain

logger = logging.getLogger(__name__)

__all__ = [
    "TelegramFileInfo",
    "TelegramStorage",
    "extract_file_info",
    "is_supported_media",
]

#: Telegram media attributes we know how to store, in priority order.
_MEDIA_ATTRS: tuple[str, ...] = (
    "document",
    "video",
    "audio",
    "voice",
    "video_note",
    "animation",
    "photo",
    "sticker",
)


@dataclass(slots=True)
class TelegramFileInfo:
    """Normalised description of an uploaded Telegram message."""

    file_id: str
    file_unique_id: str
    file_name: str
    file_size: int
    mime_type: str | None
    category: FileCategory
    caption: str | None
    duration: int | None = None
    width: int | None = None
    height: int | None = None
    thumbnail_file_id: str | None = None

    def to_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for :meth:`services.storage.StorageService.add_file`."""
        return {
            "file_id": self.file_id,
            "file_unique_id": self.file_unique_id,
            "file_name": self.file_name,
            "file_size": self.file_size,
            "mime_type": self.mime_type,
            "category": self.category,
            "caption": self.caption,
            "duration": self.duration,
            "width": self.width,
            "height": self.height,
            "thumbnail_file_id": self.thumbnail_file_id,
        }


def is_supported_media(message: Message) -> bool:
    """``True`` when the message carries a storable media object."""
    return any(getattr(message, attr, None) for attr in _MEDIA_ATTRS)


def _photo_from_message(message: Message) -> tuple[Any, FileCategory] | None:
    """Pick the largest photo size (Telegram sends several resolutions)."""
    if not message.photo:
        return None
    largest = max(message.photo, key=lambda size: size.file_size or 0)
    return largest, FileCategory.PHOTO


def extract_file_info(message: Message) -> TelegramFileInfo | None:
    """Convert an aiogram ``Message`` into a :class:`TelegramFileInfo`.

    Returns ``None`` for messages without storable media (text, service, ...).
    """
    caption = message.caption or message.text or None
    if caption:
        caption = caption[:1024]

    photo = _photo_from_message(message)
    if photo is not None:
        media, category = photo
        return TelegramFileInfo(
            file_id=media.file_id,
            file_unique_id=media.file_unique_id,
            file_name=_photo_name(message, media.file_unique_id),
            file_size=int(media.file_size or 0),
            mime_type="image/jpeg",
            category=category,
            caption=caption,
            width=getattr(media, "width", None),
            height=getattr(media, "height", None),
            thumbnail_file_id=_photo_thumbnail_id(message, chosen=media),
        )

    for attr in _MEDIA_ATTRS:
        media = getattr(message, attr, None)
        if media is None:
            continue
        file_name = (
            getattr(media, "file_name", None)
            or getattr(media, "emoji", None)
            or f"{attr}_{media.file_unique_id}"
        )
        category = _category_for(attr, str(file_name), getattr(media, "mime_type", None))
        return TelegramFileInfo(
            file_id=media.file_id,
            file_unique_id=media.file_unique_id,
            file_name=File.sanitize_filename(str(file_name), fallback=f"{attr}.bin"),
            file_size=int(getattr(media, "file_size", 0) or 0),
            mime_type=getattr(media, "mime_type", None),
            category=category,
            caption=caption,
            duration=getattr(media, "duration", None),
            width=getattr(media, "width", None),
            height=getattr(media, "height", None),
            thumbnail_file_id=_thumbnail_id(media),
        )
    return None


def _thumbnail_id(media: Any) -> str | None:
    """``file_id`` of the media thumbnail, when Telegram provides one."""
    thumbnail = getattr(media, "thumbnail", None) or getattr(media, "thumb", None)
    return getattr(thumbnail, "file_id", None) if thumbnail else None


def _photo_thumbnail_id(message: Message, *, chosen: Any) -> str | None:
    """Rasm uchun thumbnail: Telegram yuborgan **eng kichik** o'lcham.

    ``message.photo`` bir nechta o'lchamni o'z ichiga oladi (ko'tarilish
    tartibida); biz saqlash uchun eng kattasini tanlaymiz, karta preview'i
    uchun esa eng kichigi yetarli — trafik va ``getFile`` keshini tejaydi.
    Bitta o'lcham bo'lsa (juda kichik rasm) thumbnail bilan asl fayl bir xil
    bo'lib qolmasligi uchun ``None`` qaytaramiz — frontend asl rasmni baribir
    strim orqali ko'rsata olmaydi degani emas, shunchaki alohida preview shart
    emas.
    """
    sizes = list(message.photo or [])
    if len(sizes) < 2:
        return None
    smallest = min(sizes, key=lambda size: size.file_size or 0)
    if smallest.file_unique_id == getattr(chosen, "file_unique_id", None):
        return None
    return str(smallest.file_id)


def _photo_name(message: Message, unique_id: str) -> str:
    """Build a reasonable file name for a photo upload."""
    if message.caption:
        candidate = message.caption.strip().splitlines()[0]
        if "." in candidate:
            return File.sanitize_filename(candidate, fallback=f"photo_{unique_id}.jpg")
    return f"photo_{unique_id}.jpg"


def _category_for(attr: str, file_name: str, mime_type: str | None) -> FileCategory:
    """Map the Telegram media attribute onto our :class:`FileCategory`."""
    direct = {
        "video": FileCategory.VIDEO,
        "audio": FileCategory.AUDIO,
        "voice": FileCategory.VOICE,
        "video_note": FileCategory.VIDEO,
        "animation": FileCategory.ANIMATION,
        "sticker": FileCategory.OTHER,
        "photo": FileCategory.PHOTO,
    }
    if attr == "document":
        return File.detect_category(file_name, mime_type)
    return direct.get(attr, FileCategory.OTHER)


class TelegramStorage:
    """Thin async wrapper around the Telegram Bot API file endpoints."""

    def __init__(
        self,
        bot: Bot,
        *,
        cache: Cache | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.bot = bot
        self.cache = cache or get_cache()
        self.settings = settings or get_settings()

    # ------------------------------------------------------------------
    async def get_file_path(self, file_id: str, *, use_cache: bool = True) -> str:
        """Return Telegram's temporary ``file_path`` for ``file_id`` (cached)."""
        cache_key = self.cache.keys.tg_file_path(file_id)
        if use_cache:
            cached = await self.cache.get(cache_key)
            if isinstance(cached, str) and cached:
                return cached

        try:
            document = await self.bot.get_file(file_id)
        except TelegramAPIError as exc:
            logger.warning("getFile failed for %s: %s", file_id, exc)
            raise TelegramAPIErrorDomain(
                "Telegram could not resolve this file", file_id=file_id
            ) from exc

        path = document.file_path
        if not path:  # pragma: no cover - Telegram always returns one on success
            raise TelegramAPIErrorDomain("Telegram returned an empty file path", file_id=file_id)

        await self.cache.set(cache_key, path, self.settings.cache.ttl_telegram_file_path)
        return path

    def build_file_url(self, file_path: str) -> str:
        """Absolute download URL for a ``file_path``.

        Uses the local Bot API server when ``LOCAL_BOT_API`` is configured -
        that variant has no 20 MB download cap and serves files from disk.
        """
        token = self.settings.bot_token
        local = self.settings.storage.local_bot_api_server
        if local:
            return f"{local}/file/bot{token}/{file_path}"
        return f"https://api.telegram.org/file/bot{token}/{file_path}"

    async def get_file_url(self, file_id: str, *, use_cache: bool = True) -> str:
        """``get_file_path`` + :meth:`build_file_url` in one call."""
        return self.build_file_url(await self.get_file_path(file_id, use_cache=use_cache))

    async def download_url_for(self, file_id: str) -> str | None:
        """Best-effort URL, returning ``None`` instead of raising.

        Useful in inline mode where a missing thumbnail must not break results.
        """
        try:
            return await self.get_file_url(file_id)
        except TelegramAPIErrorDomain as exc:
            logger.debug("download_url_for(%s) failed: %s", file_id, exc)
            return None

    async def forget_file_path(self, file_id: str) -> None:
        """Drop a cached path (e.g. after Telegram reports it as invalid)."""
        await self.cache.delete(self.cache.keys.tg_file_path(file_id))

    async def send_stored_file(
        self,
        chat_id: int,
        file_id: str,
        *,
        category: FileCategory = FileCategory.DOCUMENT,
        caption: str | None = None,
        reply_to_message_id: int | None = None,
        reply_markup: Any | None = None,
        record: File | None = None,
    ) -> Message:
        """Send a stored file back to a chat using the right ``send*`` method.

        Re-sending by ``file_id`` costs no bandwidth: Telegram copies the object
        server-side, which is what makes the cloud "instant download" feature
        possible even for 2 GB files.

        ``record`` (the ORM row) enables the **archive vault** fallback: when
        Telegram rejects the stored ``file_id`` and the file has a copy in the
        private archive channel, the channel copy is recovered and used instead -
        so a file whose original message was deleted is still delivered.
        """
        if record is not None:
            file_id = record.best_file_id
        kwargs: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            kwargs["caption"] = caption[:1024]
        if reply_to_message_id:
            kwargs["reply_to_message_id"] = reply_to_message_id
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup

        method = {
            FileCategory.PHOTO: self.bot.send_photo,
            FileCategory.VIDEO: self.bot.send_video,
            FileCategory.AUDIO: self.bot.send_audio,
            FileCategory.VOICE: self.bot.send_voice,
            FileCategory.ANIMATION: self.bot.send_animation,
        }.get(category, self.bot.send_document)

        if method is self.bot.send_photo:
            kwargs["photo"] = file_id
        elif method is self.bot.send_video:
            kwargs["video"] = file_id
        elif method is self.bot.send_audio:
            kwargs["audio"] = file_id
        elif method is self.bot.send_voice:
            kwargs["voice"] = file_id
        elif method is self.bot.send_animation:
            kwargs["animation"] = file_id
        else:
            kwargs["document"] = file_id

        try:
            return await method(**kwargs)  # type: ignore[no-any-return]
        except TelegramAPIError as exc:
            # A stale/invalid file_id is the usual cause.
            logger.warning("send failed (%s) for %s: %s", category.value, file_id, exc)

            # 1) Archive vault recovery (only when the ORM row is available).
            recovered = await self._recover_from_vault(record, chat_id)
            if recovered:
                for candidate in (method, self.bot.send_document):
                    try:
                        return await self._send_with(candidate, chat_id, recovered, category, caption, reply_markup)
                    except TelegramAPIError:
                        continue

            # 2) Fall back to sending it as a document.
            if method is not self.bot.send_document:
                return await self.bot.send_document(
                    chat_id=chat_id, document=file_id, caption=(caption or "")[:1024] or None
                )
            raise TelegramAPIErrorDomain("Telegram refused to send this file") from exc

    async def _recover_from_vault(self, record: File | None, chat_id: int) -> str | None:
        """Ask :class:`~services.vault.ArchiveVault` for a fresh channel ``file_id``."""
        if record is None or not record.archive_message_id:
            return None
        try:
            from services.vault import get_vault

            vault = get_vault(bot=self.bot, settings=self.settings)
            if vault is None:
                return None
            return await vault.recover_file(record, to_chat_id=chat_id)
        except Exception as exc:
            logger.info("Vault recovery failed for %s: %s", record.id, exc)
            return None

    async def _send_with(
        self,
        method: Any,
        chat_id: int,
        file_id: str,
        category: FileCategory,
        caption: str | None,
        reply_markup: Any | None,
    ) -> Message:
        """Send ``file_id`` through one specific ``send_*`` method."""
        kwargs: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            kwargs["caption"] = caption[:1024]
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        if method is self.bot.send_photo:
            kwargs["photo"] = file_id
        elif method is self.bot.send_video:
            kwargs["video"] = file_id
        elif method is self.bot.send_audio:
            kwargs["audio"] = file_id
        elif method is self.bot.send_voice:
            kwargs["voice"] = file_id
        elif method is self.bot.send_animation:
            kwargs["animation"] = file_id
        else:
            kwargs["document"] = file_id
        del category
        return await method(**kwargs)  # type: ignore[no-any-return]

    async def file_size_of(self, file_id: str) -> int:
        """Ask Telegram for the size of a ``file_id`` (used to verify quota)."""
        try:
            document = await self.bot.get_file(file_id)
            return int(document.file_size or 0)
        except TelegramAPIError:  # pragma: no cover
            return 0


def new_file_uuid() -> uuid.UUID:
    """Helper kept here so bot code does not import uuid directly."""
    return uuid.uuid4()
