"""Update logging middleware.

aiogram 3 removed the old ``LoggingMiddleware``; this is a small, dependency-free
replacement that logs *what* arrived (update type, chat, user) and how long the
handler took - the two things you actually need when debugging a production bot.

Verbose HTTP-level logging of Bot API calls is available separately through
``aiogram.client.session.middlewares.RequestLogging`` (attached to the ``Bot``
session in :mod:`bot.setup`).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

logger = logging.getLogger(__name__)

__all__ = ["UpdateLoggingMiddleware"]


class UpdateLoggingMiddleware(BaseMiddleware):
    """Log every processed update with its duration."""

    def __init__(self, *, slow_threshold: float = 1.5) -> None:
        #: Updates slower than this are logged at WARNING (useful for tuning).
        self.slow_threshold = slow_threshold

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        started = time.perf_counter()
        kind = _update_kind(event)
        identity = _identity(event)
        logger.debug("→ %s %s", kind, identity)

        try:
            result = await handler(event, data)
        except Exception:
            elapsed = time.perf_counter() - started
            logger.exception("✗ %s %s failed after %.3fs", kind, identity, elapsed)
            raise

        elapsed = time.perf_counter() - started
        level = logging.WARNING if elapsed > self.slow_threshold else logging.DEBUG
        logger.log(level, "← %s %s handled in %.3fs", kind, identity, elapsed)
        return result


def _update_kind(event: TelegramObject) -> str:
    """Short label for the update type (``message``, ``callback_query``, ...)."""
    if isinstance(event, Update):
        for field in (
            "message",
            "edited_message",
            "callback_query",
            "inline_query",
            "chosen_inline_result",
            "chat_member",
            "my_chat_member",
            "poll",
            "pre_checkout_query",
        ):
            if getattr(event, field, None) is not None:
                return field
        return "update"
    return type(event).__name__.lower()


def _identity(event: TelegramObject) -> str:
    """``user=<id> chat=<id>`` when available - never logs message content."""
    parts: list[str] = []
    inner = event
    if isinstance(event, Update):
        inner = (
            event.message
            or event.edited_message
            or event.callback_query
            or event.inline_query
            or event
        )
    from_user = getattr(inner, "from_user", None)
    if from_user is not None:
        parts.append(f"user={from_user.id}")
    chat = getattr(inner, "chat", None)
    if chat is not None:
        parts.append(f"chat={chat.id}")
    message = getattr(inner, "message", None)
    if message is not None and getattr(message, "chat", None) is not None:
        parts.append(f"chat={message.chat.id}")
    return " ".join(dict.fromkeys(parts)) or "-"
