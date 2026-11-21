"""Database session + dependency injection middleware.

This is the *outermost* middleware: it opens one ``AsyncSession`` per update and
publishes every service the handlers may need into aiogram's ``data`` dict, so a
handler simply declares the parameters it wants::

    @router.message(F.photo)
    async def on_photo(
        message: Message,
        session: AsyncSession,          # from this middleware
        storage: StorageService,
        share: ShareService,
        tg_storage: TelegramStorage,
        cache: Cache,
        settings: Settings,
    ) -> None: ...

One transaction boundary per update keeps the code predictable: the session is
committed by the services (they own the write operations) and always closed here.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

from aiogram import BaseMiddleware, Bot
from aiogram.types import TelegramObject, Update

from config import Settings, get_settings
from db.database import get_session_factory
from services.cache import Cache, get_cache
from services.rate_limiter import RateLimiter, get_rate_limiter
from services.share import ShareService
from services.storage import StorageService
from services.telegram import TelegramStorage

logger = logging.getLogger(__name__)

__all__ = ["DatabaseSessionMiddleware", "get_session", "get_tg_storage"]

#: Keys used inside ``data`` - keep them in one place so handlers and tests agree.
KEY_SESSION = "session"
KEY_STORAGE = "storage"
KEY_SHARE = "share"
KEY_TG_STORAGE = "tg_storage"
KEY_CACHE = "cache"
KEY_LIMITER = "rate_limiter"
KEY_SETTINGS = "settings"


class DatabaseSessionMiddleware(BaseMiddleware):
    """Open a session per update and inject the whole service layer."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        #: Reused across updates: the object is stateless apart from the bot ref.
        self._tg_storage: TelegramStorage | None = None

    def _telegram_storage(self, bot: Bot, cache: Cache) -> TelegramStorage:
        """Lazily build (and memoise) the Telegram file helper for this bot."""
        if self._tg_storage is None or self._tg_storage.bot is not bot:
            self._tg_storage = TelegramStorage(bot, cache=cache, settings=self.settings)
        return self._tg_storage

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        bot = cast(Bot, data["bot"])
        cache = cast(Cache, data.get(KEY_CACHE) or get_cache())
        limiter = cast(RateLimiter, data.get(KEY_LIMITER) or get_rate_limiter())

        session_factory = get_session_factory()
        async with session_factory() as session:
            data[KEY_SESSION] = session
            data[KEY_CACHE] = cache
            data[KEY_LIMITER] = limiter
            data[KEY_SETTINGS] = self.settings
            data[KEY_STORAGE] = StorageService(
                session, cache=cache, settings=self.settings
            )
            data[KEY_SHARE] = ShareService(session, cache=cache, settings=self.settings)
            data[KEY_TG_STORAGE] = self._telegram_storage(bot, cache)

            try:
                return await handler(event, data)
            finally:
                # Never leak a session with a pending transaction back to the pool.
                if session.is_active and session.in_transaction():
                    try:
                        await session.rollback()
                    except Exception:  # pragma: no cover - defensive
                        logger.debug("Rollback failed while closing the update session")


def get_session(data: dict[str, Any]):
    """Typed accessor for the injected ``AsyncSession`` (used by helpers)."""
    from sqlalchemy.ext.asyncio import AsyncSession

    return cast(AsyncSession, data[KEY_SESSION])


def get_tg_storage(data: dict[str, Any]) -> TelegramStorage:
    """Typed accessor for the injected :class:`TelegramStorage`."""
    return cast(TelegramStorage, data[KEY_TG_STORAGE])


def update_chat_id(update: Update) -> int | None:
    """Best-effort chat id of an update (used for logging and throttling)."""
    for attr in ("message", "edited_message", "callback_query", "inline_query"):
        event = getattr(update, attr, None)
        if event is None:
            continue
        chat = getattr(event, "chat", None) or getattr(event, "message", None)
        if chat is not None and getattr(chat, "id", None) is not None:
            return int(chat.id)
    return None
