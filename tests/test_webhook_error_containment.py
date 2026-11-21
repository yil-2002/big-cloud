"""Webhook xato-to'ri (error containment) testlari — **deterministik**.

``tests/test_startup_smoke.py`` haqiqiy jarayonni ishga tushiradi, lekin u yerda
xato paydo bo'lishi tashqi omillarga (Telegram token'i) bog'liq. Bu fayl esa
maqsadli ravishda **yiqiladigan handler** ro'yxatdan o'tkazadi va shartnomani
tekshiradi:

1. ``POST /api/webhook`` har doim ``200 {"ok": true}`` qaytaradi (Telegram
   update'ni qayta yubormasligi uchun — "retry storm" ga yo'l qo'ymaymiz);
2. xato ``hugecloud.errors`` loggerida **kontekst bilan** qayd etiladi
   (update turi, chat, user, update_id);
3. jarayon/dispetcher keyingi update'larni qabul qilishda davom etadi.

Eslatma: bu testlar yangi ``Dispatcher`` emas, mavjud ``shared_dispatcher``
session fixture'ini ishlatadi — aiogram router'lari modul singleton bo'lgani
uchun ikkinchi dispatcher qurish butun sessiyani buzadi.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from aiogram.types import Update
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config import Settings
from main import attach_webhook
from tests.conftest import WEBHOOK_SECRET as SECRET  # umumiy qiymat (conftest)
from tests.test_webhook import FakeBot  # tarmoqqa chiqmaydigan bot stub'i


def _update_payload(update_id: int = 1, text: str = "/start", user_id: int = 777) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1_789_000_000,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Tester"},
            "text": text,
            "entities": [{"type": "bot_command", "offset": 0, "length": len(text)}],
        },
    }


@pytest.fixture
def crashing_app(session: Any, webhook_settings: Settings, shared_dispatcher: Any) -> Any:
    """Har bir message update'da istisno ko'taradigan webhook ilovasi.

    ``dispatcher.message.outer_middleware`` ga vaqtincha "bomba" o'rnatamiz:
    u har qanday handler'dan oldin yiqiladi, shu sababli xato real
    production yo'lidan (outer middleware → ErrorLoggingMiddleware) o'tadi.
    """
    from aiogram import BaseMiddleware

    class Boom(BaseMiddleware):
        async def __call__(self, handler: Any, event: Any, data: dict[str, Any]) -> Any:
            raise RuntimeError("ataylab yiqilish (test)")

    boom = Boom()
    shared_dispatcher.message.outer_middleware(boom)

    bot = FakeBot()
    app = FastAPI()
    attach_webhook(app, shared_dispatcher, bot, webhook_settings)

    try:
        with TestClient(app) as client:
            yield client, bot
    finally:
        # Middleware ro'yxatini tozalaymiz — boshqa testlarga ta'sir qilmasin.
        shared_dispatcher.message.outer_middleware.unregister(boom)


def test_handler_crash_returns_200_ok(crashing_app: Any, caplog: pytest.LogCaptureFixture) -> None:
    """Eng muhim shartnoma: xato bo'lsa ham Telegram'ga 200 qaytamiz."""
    client, _bot = crashing_app
    with caplog.at_level(logging.ERROR, logger="hugecloud.errors"):
        response = client.post(
            "/api/webhook",
            json=_update_payload(update_id=42, text="/storage"),
            headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
        )

    assert response.status_code == 200, f"webhook 500 qaytardi: {response.text[:300]}"
    assert response.json() == {"ok": True}


def test_handler_crash_is_logged_with_context(crashing_app: Any, caplog: pytest.LogCaptureFixture) -> None:
    """Xato log'da qaysi chat/buyruq ekanini ko'rsatishi kerak."""
    client, _bot = crashing_app
    with caplog.at_level(logging.ERROR, logger="hugecloud.errors"):
        client.post(
            "/api/webhook",
            json=_update_payload(update_id=4242, text="/storage", user_id=999),
            headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
        )

    text = caplog.text
    assert "Unhandled error while processing update" in text
    assert "type=message" in text
    assert "update_id=4242" in text
    assert "chat=999" in text
    assert "ataylab yiqilish" in text


def test_dispatcher_survives_after_crash(crashing_app: Any) -> None:
    """Bir xato keyingi update'larni bloklamaydi."""
    client, _bot = crashing_app
    first = client.post(
        "/api/webhook",
        json=_update_payload(update_id=1, text="/files"),
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
    )
    second = client.post(
        "/api/webhook",
        json=_update_payload(update_id=2, text="/help"),
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
    )
    assert (first.status_code, second.status_code) == (200, 200)


def test_bad_secret_still_403_even_when_handlers_crash(crashing_app: Any) -> None:
    """Autentifikatsiya xato-to'ridan **oldin** tekshiriladi."""
    client, _bot = crashing_app
    response = client.post(
        "/api/webhook",
        json=_update_payload(),
        headers={"X-Telegram-Bot-Api-Secret-Token": "not-the-secret"},
    )
    assert response.status_code == 403


def test_invalid_update_payload_is_400(crashing_app: Any) -> None:
    """Buzilgan JSON uchun 400 — lekin handler xatosi uchun emas."""
    client, _bot = crashing_app
    response = client.post(
        "/api/webhook",
        content='{"update_id": "not-a-number"}',
        headers={
            "Content-Type": "application/json",
            "X-Telegram-Bot-Api-Secret-Token": SECRET,
        },
    )
    assert response.status_code == 400


def test_update_object_reaches_middleware_chain(crashing_app: Any, caplog: pytest.LogCaptureFixture) -> None:
    """``Update`` validatsiyasi muvaffaqiyatli: xato **handler** darajasida, parse'da emas."""
    client, _bot = crashing_app
    with caplog.at_level(logging.ERROR, logger="hugecloud.errors"):
        response = client.post(
            "/api/webhook",
            json=_update_payload(update_id=77, text="/login"),
            headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
        )
    assert response.status_code == 200
    # 400 bo'lmagani = Update modeli to'g'ri yig'ilgan.
    assert "Invalid webhook payload" not in caplog.text
    # Log'da payload parchasi ham qoladi (buyruqni aniqlash uchun).
    assert "/login" in caplog.text


def test_error_middleware_registered_before_our_middlewares(shared_dispatcher: Any) -> None:
    """Xato to'ri **bizning** barcha outer middleware'lardan tashqarida turadi.

    Tartib haqida: aiogram ``Dispatcher`` konstruktorida o'zining
    ``ErrorsMiddleware`` ini eng tashqi qilib qo'yadi (xatoni ``error`` hodisasiga
    aylantiradi). Bizning ``ErrorLoggingMiddleware`` undan keyin, ammo
    ``UpdateLogging``/``DatabaseSession``/``Throttling`` dan **oldin** kelishi
    kerak — aks holda DB yoki throttling ichidagi xato log'lanmay qoladi.
    """
    from aiogram.dispatcher.middlewares.error import ErrorsMiddleware

    from bot.middlewares.db_session import DatabaseSessionMiddleware
    from bot.middlewares.logging import UpdateLoggingMiddleware
    from bot.middlewares.throttling import ThrottlingMiddleware

    middlewares = list(shared_dispatcher.update.outer_middleware)
    kinds = [type(item).__name__ for item in middlewares]

    # aiogram'ning o'rnatilgan qatlami har doim eng tashqarida.
    assert isinstance(middlewares[0], ErrorsMiddleware), kinds
    # Bizning xato to'rimiz ro'yxatdan o'tgan.
    assert "ErrorLoggingMiddleware" in kinds, kinds

    error_index = kinds.index("ErrorLoggingMiddleware")
    for cls in (UpdateLoggingMiddleware, DatabaseSessionMiddleware, ThrottlingMiddleware):
        name = cls.__name__
        if name in kinds:
            assert error_index < kinds.index(name), (
                f"{name} xato to'ridan oldin turibdi — ichidagi xato log'lanmay qoladi: {kinds}"
            )


def test_update_instance_detection() -> None:
    """``_find_update`` kontekstdan ``Update`` obyektini to'g'ri tanlaydi."""
    from bot.middlewares.error_logging import _find_update

    message_update = Update.model_validate(_update_payload(update_id=5))
    event = message_update.message
    assert isinstance(event, object)

    found = _find_update({"event_update": message_update}, event)  # type: ignore[arg-type]
    assert isinstance(found, Update)
    assert found.update_id == 5

    # Kontekstda Update bo'lmasa — hodisaning o'zi qaytadi.
    fallback = _find_update({}, event)  # type: ignore[arg-type]
    assert fallback is event
