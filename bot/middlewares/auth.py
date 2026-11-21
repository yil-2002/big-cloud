"""Authentication / registration middleware.

Runs *inside* :class:`~bot.middlewares.db_session.DatabaseSessionMiddleware`
(it needs a DB session) and guarantees that every private-chat handler receives
a persisted :class:`~db.models.user.User` row:

* unknown user  -> registered on the fly (``get_or_create_user``)
* known user    -> profile fields refreshed, ``is_blocked`` cleared
* bot was blocked by the user -> flagged, handlers stay silent

Injected keys: ``user`` (ORM row), ``is_admin`` (bool), ``user_settings`` (dict).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineQuery,
    Message,
    TelegramObject,
)
from aiogram.types import (
    User as TelegramUser,
)

from config import Settings, get_settings
from db.models import User
from services.exceptions import AppError
from services.storage import StorageService

logger = logging.getLogger(__name__)

__all__ = ["KEY_IS_ADMIN", "KEY_USER", "KEY_USER_SETTINGS", "AuthMiddleware"]

KEY_USER = "user"
KEY_IS_ADMIN = "is_admin"
KEY_USER_SETTINGS = "user_settings"


def _telegram_user(event: TelegramObject) -> TelegramUser | None:
    """Extract the acting Telegram user from any supported event type."""
    if isinstance(event, Message):
        return event.from_user
    if isinstance(event, CallbackQuery):
        return event.from_user
    if isinstance(event, InlineQuery):
        return event.from_user
    if isinstance(event, ChatMemberUpdated):
        return event.from_user
    return getattr(event, "from_user", None)


class AuthMiddleware(BaseMiddleware):
    """Resolve (or create) the ``User`` row for the current event."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.admin_ids: frozenset[int] = frozenset(self.settings.bot.admin_ids)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = _telegram_user(event)
        if tg_user is None:
            # Channel posts & friends have no acting user - let the handler decide.
            return await handler(event, data)

        storage = cast(StorageService, data["storage"])
        try:
            user, created = await storage.get_or_create_user(
                telegram_id=tg_user.id,
                username=tg_user.username,
                first_name=tg_user.first_name,
                last_name=tg_user.last_name,
                language_code=tg_user.language_code,
                is_premium=bool(tg_user.is_premium),
            )
        except Exception as exc:
            logger.exception("Auth failed for %s: %s", tg_user.id, exc)
            if isinstance(event, CallbackQuery):
                await _answer_silently(event, "Service is temporarily unavailable.")
            elif isinstance(event, Message):
                await _safe_reply(event, "⚠️ Service is temporarily unavailable, try again.")
            return None

        if created:
            logger.info("New user registered via middleware: %s", tg_user.id)

        is_admin = tg_user.id in self.admin_ids
        data[KEY_USER] = user
        data[KEY_IS_ADMIN] = is_admin
        data[KEY_USER_SETTINGS] = dict(user.settings or {})

        try:
            return await handler(event, data)
        except TelegramForbiddenError:
            # The user blocked the bot: remember it and stop sending anything.
            user.is_blocked = True
            data["session"].add(user)
            try:
                await data["session"].commit()
            except Exception:  # pragma: no cover
                logger.debug("Could not persist is_blocked for %s", user.telegram_id)
            logger.info("User %s blocked the bot", user.telegram_id)
            return None
        except AppError as exc:
            logger.warning("Domain error for %s: %s", user.telegram_id, exc.message)
            if isinstance(event, CallbackQuery):
                await _answer_silently(event, exc.message)
            elif isinstance(event, Message):
                await _safe_reply(event, f"⚠️ {exc.message}")
            return None


async def _answer_silently(callback: CallbackQuery, text: str) -> None:
    """Answer a callback with an alert, ignoring secondary failures."""
    try:
        await callback.answer(text=text[:200], show_alert=True, cache_time=5)
    except Exception:  # pragma: no cover - stale callback id
        logger.debug("callback.answer failed for %s", callback.id)


async def _safe_reply(message: Message, text: str) -> None:
    """Reply to a message, swallowing any Telegram error."""
    try:
        await message.answer(text[:4096])
    except Exception:  # pragma: no cover
        logger.debug("message.answer failed in auth middleware")


def current_user(data: dict[str, Any]) -> User | None:
    """Read the injected user (``None`` for events without an acting user)."""
    return cast(User | None, data.get(KEY_USER))
