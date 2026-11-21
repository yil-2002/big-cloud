"""Reusable aiogram filters.

Filters keep handler signatures declarative: routing decisions ("is this a
storable upload?", "is the sender an admin?", "did the user press Cancel?") live
here instead of inside handler bodies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message

from bot.keyboards.reply import BTN_CANCEL
from config import get_settings
from db.models import User
from services.telegram import extract_file_info, is_supported_media

__all__ = [
    "AdminFilter",
    "IsCancelFilter",
    "IsOwnerFilter",
    "StorableMedia",
    "TooLargeMedia",
]


class StorableMedia(BaseFilter):
    """Pass messages that carry media we can store *and* that fit the size limit.

    Injects ``media_info`` (:class:`TelegramFileInfo`) into the handler, so the
    upload handler never repeats the extraction logic::

        @router.message(StorableMedia())
        async def on_upload(message: Message, media_info: TelegramFileInfo): ...
    """

    async def __call__(self, message: Message) -> bool | dict[str, Any]:
        if not is_supported_media(message):
            return False
        info = extract_file_info(message)
        if info is None or not info.file_id:
            return False

        limit = get_settings().storage.max_file_size
        if limit and info.file_size and info.file_size > limit:
            return False
        return {"media_info": info}


class TooLargeMedia(BaseFilter):
    """Pass media messages that exceed ``STORAGE_MAX_FILE_SIZE``.

    Registered *after* :class:`StorableMedia` so oversized uploads get a precise
    explanation instead of the generic "unsupported" reply.
    """

    async def __call__(self, message: Message) -> bool | dict[str, Any]:
        if not is_supported_media(message):
            return False
        info = extract_file_info(message)
        if info is None:
            return False
        limit = get_settings().storage.max_file_size
        if not limit or not info.file_size or info.file_size <= limit:
            return False
        return {"media_info": info, "size_limit": limit}


class AdminFilter(BaseFilter):
    """Pass events sent by a configured superuser (``ADMIN_IDS``)."""

    async def __call__(self, event: Message | CallbackQuery, user: User | None = None) -> bool:
        if user is not None:
            return user.telegram_id in get_settings().bot.admin_ids
        sender = getattr(event, "from_user", None)
        return bool(sender and sender.id in get_settings().bot.admin_ids)


class IsCancelFilter(BaseFilter):
    """Pass "cancel" intents: the Cancel button, ``/cancel`` or ``/start``."""

    keywords: tuple[str, ...] = ("cancel", "отмена", "bekor", "cancel:")

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        if isinstance(event, CallbackQuery):
            data = event.data or ""
            return data == "cancel" or data.startswith("cancel:")
        text = (event.text or "").strip().lower()
        if not text:
            return False
        if text.startswith("/"):
            return text.split()[0] in {"/cancel", "/start"}
        return any(text == keyword or text.startswith(f"{keyword} ") for keyword in self.keywords) \
            or text.endswith(BTN_CANCEL.lower())


class IsOwnerFilter(BaseFilter):
    """Pass callbacks/messages whose target file belongs to the acting user.

    Expects the ORM row to be injected under ``target_file`` (handlers that
    resolve a file before routing can provide it); otherwise it is a no-op pass.
    """

    async def __call__(self, event: Message | CallbackQuery, target_file: Any | None = None) -> bool:
        if target_file is None:
            return True
        sender = getattr(event, "from_user", None)
        owner = getattr(target_file, "user_id", None)
        if sender is None or owner is None:
            return True
        return int(sender.id) == int(owner)


@dataclass(slots=True)
class CallbackStartswith(BaseFilter):
    """Cheap ``callback_data`` prefix filter (used for legacy string callbacks)."""

    prefix: str

    async def __call__(self, callback: CallbackQuery) -> bool:
        return bool(callback.data and callback.data.startswith(self.prefix))
