"""Media-group (album) collecting middleware.

Telegram delivers an album as N separate ``Message`` updates that share a
``media_group_id``. Without help, a 10-photo album triggers 10 handler calls and
10 reply messages.

This middleware buffers updates for a short window (default 600 ms - Telegram
sends an album within a few hundred milliseconds) and then calls the handler
**once** with ``album=[Message, ...]`` injected.

Only messages that actually carry media are buffered, so plain text is never
delayed. Single media messages still work exactly as before: they are delivered
after the window with ``album=[message]``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from services.telegram import is_supported_media

logger = logging.getLogger(__name__)

__all__ = ["KEY_ALBUM", "AlbumMiddleware"]

KEY_ALBUM = "album"


class AlbumMiddleware(BaseMiddleware):
    """Collect ``media_group_id`` batches and hand them over as one event."""

    def __init__(self, wait: float = 0.6) -> None:
        self.wait = max(0.05, wait)
        #: ``media_group_id -> [handler, message, data, flush task]``
        self._pending: dict[str, list[Any]] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)

        group_id = getattr(event, "media_group_id", None)
        if not group_id or not is_supported_media(event):
            # Not part of an album: deliver immediately, with a 1-item album.
            data[KEY_ALBUM] = [event]
            return await handler(event, data)

        buffer = self._pending.get(group_id)
        if buffer is None:
            task = asyncio.create_task(self._flush(group_id), name=f"album:{group_id}")
            self._pending[group_id] = [handler, [event], data, task]
            return None

        buffer[1].append(event)
        return None

    async def _flush(self, group_id: str) -> None:
        """Wait out the collection window, then invoke the handler once."""
        await asyncio.sleep(self.wait)
        buffer = self._pending.pop(group_id, None)
        if buffer is None:  # pragma: no cover - defensive
            return

        handler, messages, data, _task = buffer
        messages.sort(key=lambda message: message.message_id)
        data = dict(data)
        data[KEY_ALBUM] = messages

        try:
            await handler(messages[0], data)
        except Exception:
            logger.exception("Album handler failed for media_group_id=%s", group_id)

    async def cancel_pending(self) -> None:
        """Cancel buffered flushes (called on shutdown)."""
        for buffer in list(self._pending.values()):
            task = buffer[3]
            if not task.done():
                task.cancel()
        self._pending.clear()
