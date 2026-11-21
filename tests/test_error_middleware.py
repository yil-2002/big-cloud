"""``ErrorLoggingMiddleware`` va xato konteksti (log diagnostikasi) testlari.

Nega muhim: webhook rejimida xato Telegram'ga ``500`` sifatida qaytsa, Telegram
update'ni qayta-yuboradi (retry storm). Shu sababli middleware xatoni **log
qiladi va qayta ko'taradi**, webhook qatlami esa uni ushlab ``200`` qaytaradi.
Bu testlar shu ikki qismli shartnomani mustahkamlaydi.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineQuery,
    Message,
    Update,
    User,
)

from bot.middlewares.error_logging import ErrorLoggingMiddleware, _describe_update


# --------------------------------------------------------------------------- #
# Yordamchi obyektlar
# --------------------------------------------------------------------------- #
def _user(user_id: int = 777) -> User:
    return User(id=user_id, is_bot=False, first_name="Tester")


def _chat(chat_id: int = 777) -> Chat:
    return Chat(id=chat_id, type="private")


def _message(text: str = "/storage", message_id: int = 5) -> Message:
    return Message(
        message_id=message_id,
        date=1_789_000_000,
        chat=_chat(),
        from_user=_user(),
        text=text,
    )


def _update(message: Message | None = None, update_id: int = 42) -> Update:
    return Update(update_id=update_id, message=message or _message())


# --------------------------------------------------------------------------- #
# _describe_update
# --------------------------------------------------------------------------- #
def test_describe_update_full_context() -> None:
    description = _describe_update(_update(update_id=42))
    assert "type=message" in description
    assert "update_id=42" in description
    assert "chat=777" in description
    assert "user=777" in description
    assert "/storage" in description


def test_describe_callback_query() -> None:
    callback = CallbackQuery(
        id="cq:1",
        from_user=_user(555),
        chat_instance="-1",
        data="files::sort::created_desc",
        message=_message(),
    )
    description = _describe_update(Update(update_id=7, callback_query=callback))
    assert "type=callback_query" in description
    assert "update_id=7" in description
    assert "files::sort::created_desc" in description


def test_describe_inline_query() -> None:
    inline = InlineQuery(id="iq:1", from_user=_user(333), query="hisobot.pdf", offset="")
    description = _describe_update(Update(update_id=9, inline_query=inline))
    assert "type=inline_query" in description
    assert "hisobot.pdf" in description


def test_describe_bare_event_without_update() -> None:
    """Xato ichki hodisa darajasida yuz bersa ham kontekst yo'qolmaydi."""
    description = _describe_update(_message(text="/login ali parol123"))
    assert "type=message" in description
    assert "chat=777" in description
    assert "/login" in description


def test_describe_truncates_long_payload() -> None:
    """Uzun matn log'ni to'ldirib yubormasligi kerak (60 belgi chegarasi)."""
    description = _describe_update(_update(_message(text="x" * 500)))
    assert description.count("x") <= 60


def test_describe_never_crashes_on_unknown_object() -> None:
    class Weird:
        pass

    description = _describe_update(Weird())
    assert "type=weird" in description


def test_describe_update_with_broken_event_type() -> None:
    """``event_type`` istisno ko'tarsa ham zaxira maydon skaneri ishlaydi."""

    class BrokenUpdate(Update):
        @property
        def event_type(self) -> str:  # type: ignore[override]
            raise RuntimeError("unknown update type")

    broken = BrokenUpdate(update_id=11, message=_message())
    description = _describe_update(broken)
    assert "type=message" in description
    assert "update_id=11" in description


# --------------------------------------------------------------------------- #
# ErrorLoggingMiddleware
# --------------------------------------------------------------------------- #
async def _handler_raises(event: Any, data: dict[str, Any]) -> Any:
    raise ValueError("handler ichida kutilmagan xato")


async def _handler_ok(event: Any, data: dict[str, Any]) -> str:
    return "handled"


async def _handler_cancelled(event: Any, data: dict[str, Any]) -> Any:
    raise asyncio.CancelledError


async def test_middleware_logs_and_reraises(caplog: pytest.LogCaptureFixture) -> None:
    update = _update(update_id=42)
    middleware = ErrorLoggingMiddleware()
    with caplog.at_level(logging.ERROR, logger="hugecloud.errors"), pytest.raises(
        ValueError, match="kutilmagan xato"
    ):
        await middleware(_handler_raises, update, {"bot": None, "event_update": update})

    assert "Unhandled error while processing update" in caplog.text
    assert "update_id=42" in caplog.text
    assert "chat=777" in caplog.text


async def test_middleware_passes_through_success(caplog: pytest.LogCaptureFixture) -> None:
    update = _update()
    middleware = ErrorLoggingMiddleware()
    with caplog.at_level(logging.ERROR, logger="hugecloud.errors"):
        result = await middleware(_handler_ok, update, {"event_update": update})
    assert result == "handled"
    assert "Unhandled error" not in caplog.text


async def test_middleware_does_not_swallow_cancellation() -> None:
    """Graceful shutdown signali xato deb hisoblanmasligi kerak."""
    update = _update()
    middleware = ErrorLoggingMiddleware()
    with pytest.raises(asyncio.CancelledError):
        await middleware(_handler_cancelled, update, {"event_update": update})


async def test_middleware_finds_update_in_context() -> None:
    """Hodisa ``Message`` bo'lsa ham kontekstdagi ``Update`` topiladi."""
    message = _message(text="/files")
    update = Update(update_id=88, message=message)
    middleware = ErrorLoggingMiddleware()

    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    logger = logging.getLogger("hugecloud.errors")
    handler = Capture()
    logger.addHandler(handler)
    try:
        with pytest.raises(ValueError):
            # ``event`` = Message, lekin ``data`` da Update bor.
            await middleware(_handler_raises, message, {"event_update": update})
    finally:
        logger.removeHandler(handler)

    assert records, "xato log'lanmadi"
    assert "update_id=88" in records[0]
    assert "type=message" in records[0]


# --------------------------------------------------------------------------- #
# AuthGuard: Telegram javob bermasa ham update yiqilmasligi kerak
# --------------------------------------------------------------------------- #
async def test_auth_guard_reject_swallows_telegram_failure() -> None:
    """``message.answer`` xatosi rad javobini yiqitmasligi kerak (foydalanuvchi yo'qolmasin)."""
    from unittest.mock import AsyncMock

    from bot.middlewares.auth_guard import AuthGuardMiddleware
    from config import get_settings

    guard = AuthGuardMiddleware(get_settings())
    message = _message(text="/storage")

    failing_answer = AsyncMock(side_effect=RuntimeError("Telegram unreachable"))
    original = type(message).answer
    type(message).answer = failing_answer  # type: ignore[method-assign]
    try:
        # Istisno tashqariga chiqmasligi kerak.
        await guard._reject(message, "<b>Kiring</b>")
    finally:
        type(message).answer = original  # type: ignore[method-assign]

    failing_answer.assert_awaited_once()


async def test_auth_guard_reject_callback_swallows_failure() -> None:
    """Callback alert javobi ham xatoni yutadi (eskirgan callback_id holatlari)."""
    from unittest.mock import AsyncMock

    from aiogram.types import CallbackQuery

    from bot.middlewares.auth_guard import AuthGuardMiddleware
    from config import get_settings

    callback = CallbackQuery(
        id="cq:9",
        from_user=_user(123),
        chat_instance="-1",
        data="files::page::2",
        message=_message(),
    )
    failing = AsyncMock(side_effect=RuntimeError("query is too old"))
    original = type(callback).answer
    type(callback).answer = failing  # type: ignore[method-assign]
    try:
        await AuthGuardMiddleware(get_settings())._reject(callback, "Rad etildi")
    finally:
        type(callback).answer = original  # type: ignore[method-assign]

    failing.assert_awaited_once()
