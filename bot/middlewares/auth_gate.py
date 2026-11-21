"""🔐 Password gate middleware (legacy ``BOT_PASSWORD`` behaviour).

Position in the chain: **after** :class:`~bot.middlewares.auth.AuthMiddleware`
(it needs the persisted ``User``) and **before** every handler.

Behaviour:

* ``BOT_PASSWORD`` empty  -> the middleware is a no-op (open bot);
* user already authorized -> pass through untouched;
* admin ids               -> always pass through;
* otherwise               -> set ``PasswordState.waiting_password``, ask for the
  password and swallow the update. The handler in
  :mod:`bot.handlers.browsing` verifies it (constant-time compare, attempt
  counter with a cooldown stored in FSM data).

This mirrors the original bot, with two deliberate improvements: the
authorization survives restarts, and wrong attempts are rate-limited.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot import texts
from bot.keyboards.reply import cancel_reply_keyboard
from bot.states import PasswordState
from config import Settings, get_settings
from db.models import User
from services.storage import StorageService

logger = logging.getLogger(__name__)

__all__ = ["PasswordGateMiddleware"]


class PasswordGateMiddleware(BaseMiddleware):
    """Block unauthenticated users until they enter the shared password."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def enabled(self) -> bool:
        """``True`` when a shared password is configured."""
        return bool(self.settings.bot.password_value())

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not self.enabled:
            return await handler(event, data)

        user = cast(User | None, data.get("user"))
        if user is None:  # pragma: no cover - no acting user (channel post)
            return await handler(event, data)

        storage = cast(StorageService, data["storage"])
        if storage.is_authorized(user):
            return await handler(event, data)

        # The password input itself must reach the handler.
        state = cast(FSMContext | None, data.get("state"))
        if state is not None:
            current = await state.get_state()
            if current == PasswordState.waiting_password:
                return await handler(event, data)

        await self._ask(event, state)
        return None

    async def _ask(self, event: TelegramObject, state: FSMContext | None) -> None:
        """Park the user in the password state and prompt them."""
        if state is not None:
            await state.set_state(PasswordState.waiting_password)
            await state.update_data({"password_attempts": 0})

        if isinstance(event, CallbackQuery):
            # A button press from an unauthorized user: modal alert, no message.
            try:
                await event.answer(texts.auth_required(), show_alert=True, cache_time=5)
            except Exception:  # pragma: no cover - stale callback
                logger.debug("callback.answer failed in the password gate")
            return

        if isinstance(event, Message):
            try:
                await event.answer(
                    f"{texts.auth_required()}\n{texts.password_prompt()}",
                    reply_markup=cancel_reply_keyboard(),
                )
            except Exception:  # pragma: no cover
                logger.debug("Could not send the password prompt")
