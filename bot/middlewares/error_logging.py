"""Update darajasidagi xatolarni **kontekst bilan** log qiluvchi middleware.

Muammo
------
Handler yoki middleware ichida yuz bergan istisno ``dispatcher.feed_update`` dan
tashqariga chiqadi. Webhook rejimida bu Telegram'ga ``500`` qaytarishiga,
natijada Telegram xuddi shu update'ni qayta-qayta yuborishiga olib keladi
("retry storm"). Polling rejimida esa aiogram xatoni yuqori darajada log qiladi,
ammo **qaysi foydalanuvchi, qaysi buyruq** ekanini ko'rish qiyin.

Yechim
------
Bu middleware **bizning** eng tashqi qatlamimiz (aiogram'ning o'rnatilgan
``ErrorsMiddleware`` i undan ham tashqarida turadi va xatoni ``error`` hodisasiga
aylantirib, maxsus handler topilmasa qayta ko'taradi):

1. istisnoni foydalanuvchi/sohna/update turi bilan birga log qiladi;
2. ``CancelledError`` ni (graceful shutdown) o'tkazib yuboradi;
3. xatoni **qayta ko'taradi** — ya'ni webhook qatlami uni ushlab Telegram'ga
   ``{"ok": true}`` qaytaradi va update qayta yuborilmaydi.

Shunday qilib bitta nuqtada ham diagnostika, ham "cheksiz retry" ga yo'l qo'ymaslik
ta'minlanadi.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.types import TelegramObject, Update

logger = logging.getLogger("hugecloud.errors")

__all__ = ["ErrorLoggingMiddleware", "_describe_update"]


#: Update turlari (``event_type`` hisoblanmagan holatlar uchun zaxira).
_UPDATE_FIELDS: tuple[str, ...] = (
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "inline_query",
    "chosen_inline_result",
    "callback_query",
    "shipping_query",
    "pre_checkout_query",
    "poll",
    "poll_answer",
    "my_chat_member",
    "chat_member",
    "chat_join_request",
)


def _update_kind(update: Any) -> str:
    """Update turini aniqlash (``event_type`` xato bersa ham ishlashi kerak)."""
    try:
        kind = update.event_type
    except Exception:
        kind = None
    if kind:
        return str(kind)
    for field in _UPDATE_FIELDS:
        if getattr(update, field, None) is not None:
            return field
    return "unknown"


def _describe_update(obj: Any) -> str:
    """Update yoki hodisa obyektini inson o'qiy oladigan qisqa satrga aylantirish.

    Hech qachon fayl mazmunini yoki parolni log qilmaydi: faqat tur, id va
    matnning birinchi 60 belgisi.
    """
    event: Any
    if isinstance(obj, Update):
        try:
            event: Any = obj.event
        except Exception:
            event = next(
                (getattr(obj, field) for field in _UPDATE_FIELDS if getattr(obj, field, None) is not None),
                None,
            )
        kind = _update_kind(obj)
        update_id = obj.update_id
    else:
        # Ba'zan xato ichki hodisa (Message/CallbackQuery) darajasida yuz beradi.
        event = obj
        kind = type(obj).__name__.lower()
        update_id = getattr(obj, "update_id", "?")

    chat_id = getattr(getattr(event, "chat", None), "id", None)
    sender = getattr(event, "from_user", None) or getattr(event, "sender", None)
    user_id = getattr(sender, "id", None)
    payload: Any = None
    for field in ("text", "data", "query", "caption", "game_short_name"):
        payload = getattr(event, field, None)
        if payload:
            break
    snippet = str(payload)[:60].replace("\n", " ") if payload else ""
    return f"type={kind} update_id={update_id} chat={chat_id} user={user_id} payload={snippet!r}"


def _find_update(data: dict[str, Any], event: TelegramObject) -> Any:
    """Kontekstdagi ``Update`` obyektini topish (aks holda hodisaning o'zi)."""
    for key in ("event_update", "update", "raw_update"):
        candidate = data.get(key)
        if isinstance(candidate, Update):
            return candidate
    for value in data.values():
        if isinstance(value, Update):
            return value
    return event


class ErrorLoggingMiddleware(BaseMiddleware):
    """Har qanday update xatosini log qiladi va qayta ko'taradi (suppress qilmaydi)."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except asyncio.CancelledError:
            # Graceful shutdown — bu xato emas, yuqoriga o'tkazamiz.
            raise
        except Exception as exc:
            bot: Bot | None = data.get("bot")
            bot_id = getattr(bot, "id", "?")
            # ``logger.error(..., exc_info=True)``: traceback **bir marta** chiqadi
            # (``logger.exception`` + ``exc`` argumenti xabarni takrorlardir).
            logger.error(  # noqa: G201 - kontekst va traceback bitta yozuvda bo'lsin
                "Unhandled error while processing update (bot=%s) %s -> %s: %s",
                bot_id,
                _describe_update(_find_update(data, event)),
                exc.__class__.__name__,
                exc,
                exc_info=True,
            )
            # Qayta ko'taramiz: webhook qatlami uni ushlab 200 qaytaradi,
            # polling qatlami esa aiogram'ning o'z himoyasiga tayanadi.
            raise
