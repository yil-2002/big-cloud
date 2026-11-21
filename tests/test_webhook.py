"""🪝 Webhook rejimi: marshrut FastAPI ichida, secret tekshiruvi, update oqimi.

Nega bu test muhim: ``RUN_MODE=all`` + ``WEBHOOK_ENABLED=true`` da API va bot
**bitta portda** ishlashi kerak (PaaS faqat bitta HTTP port beradi). Avval
alohida aiohttp server ishlatilgan edi — u uvicorn bilan port uchun urishardi.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram import Bot

from main import attach_webhook, register_webhook
from tests.conftest import WEBHOOK_PATH  # umumiy qiymat (conftest)
from tests.conftest import WEBHOOK_SECRET as SECRET  # umumiy qiymat (conftest)


class FakeBot(Bot):
    """Haqiqiy ``Bot`` turi, lekin tarmoqqa chiqmaydi.

    ``isinstance(bot, Bot)`` tekshiruvlari (masalan ``message.answer`` ichida)
    ishlayishi uchun haqiqiy sinfdan meros olamiz, ammo barcha API chaqiruvlari
    yozib olinadi.
    """

    def __init__(self) -> None:
        super().__init__(token="123456:TEST-TOKEN")
        self.sent: list[dict[str, Any]] = []
        self.webhook_calls: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> Any:
        self.sent.append(kwargs)
        return None

    async def set_webhook(self, **kwargs: Any) -> bool:
        self.webhook_calls.append(kwargs)
        return True

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """``message.answer`` va boshqa metodlar shu yerdan o'tadi."""
        method = args[0] if args else kwargs.get("method")
        self.sent.append({"method": type(method).__name__, **method.model_dump(exclude_none=True)})
        return None

    async def __aenter__(self) -> FakeBot:  # noqa: PYI034 - stub o'zini qaytaradi
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


def _update_payload(text: str = "/start", telegram_id: int = 777) -> dict[str, Any]:
    """Telegram yuboradigan ``Update`` JSON ko'rinishi."""
    return {
        "update_id": 100000001,
        "message": {
            "message_id": 1,
            "date": int(datetime.now(UTC).timestamp()),
            "chat": {"id": telegram_id, "type": "private", "first_name": "Tester"},
            "from": {"id": telegram_id, "is_bot": False, "first_name": "Tester"},
            "text": text,
            "entities": [{"type": "bot_command", "offset": 0, "length": len(text)}]
            if text.startswith("/")
            else [],
        },
    }


@pytest.fixture()
def webhook_app(session, webhook_settings, shared_dispatcher):
    """Webhook marshruti ulangan FastAPI ilovasi + dispetcher + bot."""
    from fastapi.testclient import TestClient

    from api.app import create_app

    app = create_app(webhook_settings)
    bot = FakeBot()
    attach_webhook(app, shared_dispatcher, bot, webhook_settings)
    with TestClient(app) as client:
        yield client, app, shared_dispatcher, bot, webhook_settings


# --------------------------------------------------------------------------- #
# Marshrut va xavfsizlik
# --------------------------------------------------------------------------- #
def test_webhook_route_is_registered(webhook_app) -> None:
    """``POST /api/webhook`` ilovada mavjud (alohida server yo'q)."""
    client, app, *_ = webhook_app
    paths = {getattr(route, "path", None) for route in app.routes}
    assert WEBHOOK_PATH in paths
    # Health-check va Mini App ham shu portda qoladi.
    assert client.get("/healthz").status_code == 200


def test_webhook_rejects_wrong_secret(webhook_app) -> None:
    """Noto'g'ri ``X-Telegram-Bot-Api-Secret-Token`` → 403 (update ishlanmaydi)."""
    client, _app, _dispatcher, bot, _ = webhook_app
    response = client.post(
        WEBHOOK_PATH,
        json=_update_payload(),
        headers={"X-Telegram-Bot-Api-Secret-Token": "noto-gri"},
    )
    assert response.status_code == 403
    assert bot.sent == []


def test_webhook_rejects_missing_secret(webhook_app) -> None:
    """Secret so'ralgan bo'lsa, sarlavhasiz so'rov ham rad etiladi."""
    client, *_rest = webhook_app
    response = client.post(WEBHOOK_PATH, json=_update_payload())
    assert response.status_code == 403


def test_webhook_rejects_invalid_payload(webhook_app) -> None:
    """Buzilgan JSON → 400 (500 emas, Telegram qayta yubormasligi uchun)."""
    client, *_rest = webhook_app
    response = client.post(
        WEBHOOK_PATH,
        json={"update_id": "bu-son-emas"},
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
    )
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# Update oqimi (haqiqiy handler'lar)
# --------------------------------------------------------------------------- #
def test_webhook_processes_start_update(webhook_app) -> None:
    """``/start`` update'i dispetcher orqali handler'ga yetib boradi va javob yuboriladi."""
    client, _app, _dispatcher, bot, _ = webhook_app
    response = client.post(
        WEBHOOK_PATH,
        json=_update_payload("/start"),
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True}
    # /start handler'i javob yubordi (kamida bitta chaqiruv).
    assert bot.sent, "bot hech qanday javob yubormadi"


def test_webhook_processes_plain_message(webhook_app) -> None:
    """Matnli xabar ham ishlanadi (auth darvozasi javob beradi)."""
    client, _app, _dispatcher, bot, _ = webhook_app
    response = client.post(
        WEBHOOK_PATH,
        json=_update_payload("salom"),
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
    )
    assert response.status_code == 200, response.text
    assert bot.sent


def test_webhook_without_secret_configured_is_open(
    session, settings, shared_dispatcher
) -> None:
    """``WEBHOOK_SECRET`` bo'sh bo'lsa sarlavha talab qilinmaydi (dev rejim)."""
    from fastapi.testclient import TestClient

    from api.app import create_app

    settings.api.webhook_secret = ""
    settings.api.webhook_path = WEBHOOK_PATH
    app = create_app(settings)
    bot = FakeBot()
    attach_webhook(app, shared_dispatcher, bot, settings)

    with TestClient(app) as client:
        response = client.post(WEBHOOK_PATH, json=_update_payload("/start"))
        assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- #
# setWebhook chaqiruvi
# --------------------------------------------------------------------------- #
async def test_register_webhook_builds_full_url(
    webhook_settings, shared_dispatcher
) -> None:
    """``WEBHOOK_URL`` + ``WEBHOOK_PATH`` birlashtirilib Telegram'ga yuboriladi."""
    bot = FakeBot()
    await register_webhook(webhook_settings, shared_dispatcher, bot)

    assert len(bot.webhook_calls) == 1
    call = bot.webhook_calls[0]
    assert call["url"] == "https://hugecloud.example.com/api/webhook"
    assert call["secret_token"] == SECRET
    assert isinstance(call["allowed_updates"], list)
    # Polling bilan bir vaqtda ishlamasligi uchun pending update'lar tashlanadi.
    assert call["drop_pending_updates"] is True


async def test_register_webhook_requires_url(settings, shared_dispatcher) -> None:
    """``WEBHOOK_URL`` bo'sh bo'lsa aniq xato beriladi (jim ishlamaydi)."""
    settings.api.webhook_enabled = True
    settings.api.webhook_url = ""
    bot = FakeBot()
    with pytest.raises(ValueError, match="WEBHOOK_URL"):
        await register_webhook(settings, shared_dispatcher, bot)


async def test_webhook_and_api_share_one_port(
    webhook_settings, shared_dispatcher
) -> None:
    """Asosiy kafolat: webhook API ilovasida, **alohida port band qilinmaydi**."""
    from api.app import create_app

    app = create_app(webhook_settings)
    attach_webhook(app, shared_dispatcher, FakeBot(), webhook_settings)

    # Router'lar prefiks bilan ulanadi, shuning uchun yo'llarni OpenAPI
    # sxemasidan olamiz (``app.routes`` ichida ``_IncludedRouter`` ko'rinadi).
    openapi_paths = set(app.openapi()["paths"])
    direct_paths = {getattr(route, "path", None) for route in app.routes}
    # Bitta ilovada ham API, ham Mini App, ham webhook — bitta port.
    assert "/api/v1/system/health" in openapi_paths
    assert "/healthz" in direct_paths
    assert "/" in direct_paths
    assert WEBHOOK_PATH in direct_paths


# --------------------------------------------------------------------------- #
# Regressiya: haqiqiy handler'lar update'da yiqilmasligi kerak
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "/start",
        "/help",
        "/id",
        "/storage",          # or_f(Command, F.text.in_) — avval TypeError berardi
        "/search",
        "/stats",
        "/settings",
        "/share",
        "/webapp",
        "/upload",
        "/files",
        "/register",
        "/login",
        "salom",             # noma'lum matn handler'i
    ],
)
def test_every_command_survives_dispatch(webhook_app, text: str) -> None:
    """Har bir buyruq dispetcher orqali o'tadi (filtr xatolari shu yerda ochiladi).

    aiogram 3.31'da ``Command("x") | F.text.in_(...)`` aralashmasi
    ``TypeError: unsupported operand type(s) for |: 'Command' and 'bool'``
    berardi — xato faqat update keliganda ko'rinadi, import vaqtida emas.
    """
    client, _app, _dispatcher, _bot, _ = webhook_app
    response = client.post(
        WEBHOOK_PATH,
        json=_update_payload(text),
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
    )
    assert response.status_code == 200, f"{text}: {response.text}"


def test_button_text_variants_survive_dispatch(webhook_app) -> None:
    """Reply-tugma matnlari (emoji bilan) ham handler'ga yetib boradi."""
    client, _app, _dispatcher, _bot, _ = webhook_app
    from bot.handlers.commands import _button_texts
    from bot.utils.emojis import Emoji

    variants = [
        *_button_texts(Emoji.FOLDER_OPEN, "Storage"),
        *_button_texts(Emoji.SEARCH, "Search"),
        *_button_texts(Emoji.STATS, "Stats"),
    ]
    for text in variants:
        response = client.post(
            WEBHOOK_PATH,
            json=_update_payload(text),
            headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
        )
        assert response.status_code == 200, f"{text!r}: {response.text}"


# --------------------------------------------------------------------------- #
# Webhook ro'yxatdan o'tkazish: qayta urinishlar
# --------------------------------------------------------------------------- #
async def test_register_webhook_retries_on_network_error(webhook_settings, shared_dispatcher) -> None:
    """Telegram javob bermasa — qayta urinadi, jarayonni o'chirmaydi."""
    import main as main_module

    class FlakyBot(FakeBot):
        def __init__(self, failures: int) -> None:
            super().__init__()
            self.failures = failures

        async def set_webhook(self, **kwargs: Any) -> bool:
            if self.failures > 0:
                self.failures -= 1
                raise RuntimeError("Telegram is not responding")
            return await super().set_webhook(**kwargs)

    bot = FlakyBot(failures=2)
    # Pauzalarni testda kutmaslik uchun nolga yaqinlashtiramiz.
    original = main_module.WEBHOOK_BACKOFF
    main_module.WEBHOOK_BACKOFF = (0.0, 0.0, 0.0, 0.0)
    try:
        await register_webhook(webhook_settings, shared_dispatcher, bot)
    finally:
        main_module.WEBHOOK_BACKOFF = original

    assert len(bot.webhook_calls) == 1                 # 3-urinishda muvaffaqiyat
    assert bot.webhook_calls[0]["url"].endswith("/api/webhook")


async def test_register_webhook_gives_up_after_max_attempts(webhook_settings, shared_dispatcher) -> None:
    """``max_attempts`` chegarasi: cheksiz qayta urinish bo'lmaydi."""
    import main as main_module

    class DeadBot(FakeBot):
        async def set_webhook(self, **kwargs: Any) -> bool:
            raise RuntimeError("no route to Telegram")

    original = main_module.WEBHOOK_BACKOFF
    main_module.WEBHOOK_BACKOFF = (0.0, 0.0, 0.0, 0.0)
    try:
        # Istisno tashqariga chiqmaydi — API ishlashda davom etishi kerak.
        await register_webhook(webhook_settings, shared_dispatcher, DeadBot(), max_attempts=3)
    finally:
        main_module.WEBHOOK_BACKOFF = original
