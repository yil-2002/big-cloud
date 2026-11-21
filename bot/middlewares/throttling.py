"""Throttling middleware for Telegram updates.

Uses the shared sliding-window limiter from :mod:`services.rate_limiter` so bot
and HTTP API obey the same policy engine (and share state when Redis is up).

Scopes and their settings::

    message   -> BOT_RATE_LIMIT      / BOT_RATE_WINDOW
    callback  -> CALLBACK_RATE_LIMIT / CALLBACK_RATE_WINDOW
    inline    -> INLINE_RATE_LIMIT   / INLINE_RATE_WINDOW

Behaviour on denial:

* callback query -> ``answer(show_alert=True)`` with the remaining wait time and
  the handler is **not** called (prevents callback floods);
* inline query   -> ignored silently (answering would still cost a request);
* message        -> a warning is sent **at most once per window** per user, so a
  spammer cannot make the bot spam itself.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, InlineQuery, Message, TelegramObject

from bot.utils.emojis import Emoji
from config import Settings, get_settings
from services.rate_limiter import RateLimiter, RateLimitResult, get_rate_limiter

logger = logging.getLogger(__name__)

__all__ = ["SCOPE_CALLBACK", "SCOPE_INLINE", "SCOPE_MESSAGE", "ThrottlingMiddleware"]

SCOPE_MESSAGE = "bot"
SCOPE_CALLBACK = "callback"
SCOPE_INLINE = "inline"


class ThrottlingMiddleware(BaseMiddleware):
    """Per-user, per-scope sliding window limiter."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        #: ``telegram_id -> monotonic timestamp`` of the last warning we sent.
        self._last_warning: dict[int, float] = {}

    @property
    def enabled(self) -> bool:
        """Master switch (``RATE_LIMIT_ENABLED=false`` disables everything)."""
        return self.settings.security.rate_limit_enabled

    def _limits_for(self, scope: str) -> tuple[int, int]:
        """Return ``(limit, window)`` configured for a scope."""
        security = self.settings.security
        if scope == SCOPE_CALLBACK:
            return security.callback_rate_limit, security.callback_rate_window
        if scope == SCOPE_INLINE:
            return security.inline_rate_limit, security.inline_rate_window
        return security.bot_rate_limit, security.bot_rate_window

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not self.enabled:
            return await handler(event, data)

        scope = _scope_of(event)
        if scope is None:
            return await handler(event, data)

        identity = _identity_of(event)
        if identity is None:  # pragma: no cover - anonymous channel updates
            return await handler(event, data)

        limiter = cast(RateLimiter, data.get("rate_limiter") or get_rate_limiter())
        limit, window = self._limits_for(scope)
        result = await limiter.check(scope, identity, limit=limit, window=window)

        if result.allowed:
            return await handler(event, data)

        await self._deny(event, scope, result)
        return None

    # ------------------------------------------------------------------
    async def _deny(self, event: TelegramObject, scope: str, result: RateLimitResult) -> None:
        """React to an over-quota event without ever raising."""
        logger.info("Throttled %s for %s (retry in %ss)", scope, result.identity, result.retry_after)

        if isinstance(event, CallbackQuery):
            try:
                await event.answer(
                    text=(
                        f"{Emoji.CLOCK} Too fast! Please wait {result.retry_after} s."
                    )[:200],
                    show_alert=False,
                    cache_time=result.retry_after,
                )
            except Exception:  # pragma: no cover - stale callback
                logger.debug("callback.answer failed while throttling")
            return

        if isinstance(event, InlineQuery):
            # Answering with zero results is pointless; just drop the query.
            return

        if isinstance(event, Message):
            await self._warn_once(event, result)

    async def _warn_once(self, message: Message, result: RateLimitResult) -> None:
        """Send at most one throttle warning per window per user."""
        user_id = message.from_user.id if message.from_user else 0
        now = time.monotonic()
        last = self._last_warning.get(user_id, 0.0)
        if now - last < result.retry_after:
            return
        self._last_warning[user_id] = now

        # Opportunistic GC so the dict cannot grow without bound.
        if len(self._last_warning) > 10_000:
            cutoff = now - 3600
            self._last_warning = {k: v for k, v in self._last_warning.items() if v > cutoff}

        try:
            await message.answer(
                f"{Emoji.HOURGLASS} <b>Slow down a bit!</b>\n"
                f"Limit: <b>{result.limit}</b> requests per "
                f"<b>{self._window_text(result.scope)}</b>.\n"
                f"Try again in <b>{result.retry_after} s</b>.",
            )
        except Exception:  # pragma: no cover
            logger.debug("Could not deliver the throttle warning to %s", user_id)

    def _window_text(self, scope: str) -> str:
        """Human readable window length used in the warning message."""
        _, window = self._limits_for(scope)
        if window % 60 == 0:
            return f"{window // 60} min"
        return f"{window} sec"


def _scope_of(event: TelegramObject) -> str | None:
    """Map an update type onto a throttling scope."""
    if isinstance(event, CallbackQuery):
        return SCOPE_CALLBACK
    if isinstance(event, InlineQuery):
        return SCOPE_INLINE
    if isinstance(event, Message):
        return SCOPE_MESSAGE
    return None


def _identity_of(event: TelegramObject) -> str | None:
    """Stable per-user identity; falls back to chat id for anonymous senders."""
    from_user = getattr(event, "from_user", None)
    if from_user is not None and getattr(from_user, "id", None):
        return f"u{from_user.id}"
    chat = getattr(event, "chat", None)
    if chat is not None and getattr(chat, "id", None):
        return f"c{chat.id}"
    return None
