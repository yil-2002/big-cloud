"""Bot-side facade over :mod:`services.telegram`.

Handlers import from here so the aiogram layer stays decoupled from the service
package: everything Telegram-specific (``getFile``, download URLs, re-sending a
stored file by ``file_id``) is reachable through one small surface, and the
dependency-injected instance arrives via ``bot/utils`` middleware data.

Typical handler code::

    async def download_handler(message: Message, tg_storage: TelegramStorage):
        url = await tg_storage.get_file_url(record.file_id)

The middleware registers the object under the ``tg_storage`` key (see
``bot/middlewares/db_session.py``), so aiogram injects it automatically.
"""

from __future__ import annotations

from typing import Any

from aiogram import Bot
from aiogram.types import Message

from config import Settings, get_settings
from db.models import File, FileCategory
from services.cache import Cache, get_cache
from services.telegram import (
    TelegramFileInfo,
    extract_file_info,
    is_supported_media,
)
from services.telegram import (
    TelegramStorage as _TelegramStorageService,
)

__all__ = [
    "TelegramFileInfo",
    "TelegramStorage",
    "extract_file_info",
    "is_supported_media",
]


class TelegramStorage(_TelegramStorageService):
    """Service subclass with convenience wrappers used by handlers.

    Adds message-level helpers (``store_from_message``-friendly payloads) and
    keeps the same constructor signature so it can be created in the middleware
    factory with ``(bot, cache=..., settings=...)``.
    """

    def __init__(
        self,
        bot: Bot,
        *,
        cache: Cache | None = None,
        settings: Settings | None = None,
    ) -> None:
        super().__init__(bot, cache=cache or get_cache(), settings=settings or get_settings())

    # ------------------------------------------------------------------
    async def reply_with_file(
        self,
        message: Message,
        record: File,
        *,
        caption: str | None = None,
        reply_markup: Any | None = None,
    ) -> Message:
        """Send a stored file back into the chat the message came from."""
        return await self.send_stored_file(
            chat_id=message.chat.id,
            file_id=record.best_file_id,
            category=record.category,
            caption=caption or record.caption,
            reply_to_message_id=message.message_id,
            reply_markup=reply_markup,
            record=record,
        )

    async def deliver(
        self,
        chat_id: int,
        record: File,
        *,
        caption: str | None = None,
        reply_markup: Any | None = None,
    ) -> Message:
        """Deliver a file to an arbitrary chat (deep-link downloads)."""
        return await self.send_stored_file(
            chat_id=chat_id,
            file_id=record.best_file_id,
            category=record.category or FileCategory.DOCUMENT,
            caption=caption,
            reply_markup=reply_markup,
            record=record,
        )

    @staticmethod
    def info_from(message: Message) -> TelegramFileInfo | None:
        """Alias of :func:`extract_file_info` for readability in handlers."""
        return extract_file_info(message)

    async def resolve_download(self, record: File) -> tuple[str, str] | None:
        """Return ``(file_path, url)`` for a stored file, or ``None`` on failure.

        Handlers use this to build "download in browser" buttons without caring
        about caching or the local Bot API server.
        """
        try:
            path = await self.get_file_path(record.best_file_id)
        except Exception:
            return None
        return path, self.build_file_url(path)
