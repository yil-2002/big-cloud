"""🔗 Mini App menyu tugmasi: ``setChatMenuButton`` har startup'da yangilanadi.

Audit muammosi: ``scripts/tunnel.sh`` ``.env`` dagi ``BASE_URL`` ni yangilardi,
lekin ``setChatMenuButton`` chaqirilmasdi — Telegram'dagi menyu tugmasi eski
(o'lik) manzilga ishora qilib qolardi va Mini App ochilmasdi.

Bu testlar shartnomani qulflaydi:
* faqat **HTTPS** manzil yuboriladi (Telegram ``http://`` ni rad etadi);
* xato startup'ni **o'ldirmaydi** (webhook qattiqligidagi kabi);
* flag o'chirilsa hech narsa qilinmaydi;
* ``main.py`` funksiyani ``register_commands`` dan **keyin** chaqiradi.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bot.setup import ensure_menu_button
from config import Settings, get_settings

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.asyncio


class StubBot:
    """``set_chat_menu_button`` chaqiruvlarini yozib oladigan bot."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.calls: list[Any] = []
        self._fail = fail

    async def set_chat_menu_button(self, chat_id: int | None = None, menu_button: Any = None) -> bool:
        if self._fail is not None:
            raise self._fail
        self.calls.append({"chat_id": chat_id, "menu_button": menu_button})
        return True


async def test_https_url_is_registered(settings: Settings) -> None:
    """To'g'ri HTTPS manzil Telegram'ga yoziladi."""
    settings.web.set_menu_button = True
    settings.web.web_app_url = "https://77-42-77-60.sslip.io/"
    bot = StubBot()

    assert await ensure_menu_button(bot, settings) is True
    assert len(bot.calls) == 1
    call = bot.calls[0]
    assert call["chat_id"] is None, "asosiy (default) menyu tugmasi o'rnatiladi"
    assert call["menu_button"].type == "web_app"
    assert call["menu_button"].web_app.url == "https://77-42-77-60.sslip.io/"
    assert call["menu_button"].text == settings.web.menu_button_text


async def test_http_url_is_rejected(settings: Settings) -> None:
    """``http://`` manzil yuborilmaydi — Telegram uni baribir rad etadi."""
    settings.web.set_menu_button = True
    settings.web.web_app_url = "http://localhost:8000/"
    bot = StubBot()

    assert await ensure_menu_button(bot, settings) is False
    assert bot.calls == [], "HTTPS bo'lmagan manzil Telegram'ga ketmasligi kerak"


async def test_empty_url_is_rejected(settings: Settings) -> None:
    """Bo'sh manzil bilan chaqiruv qilinmaydi."""
    settings.web.set_menu_button = True
    settings.web.web_app_url = ""
    bot = StubBot()

    assert await ensure_menu_button(bot, settings) is False
    assert bot.calls == []


async def test_flag_disabled_skips_call(settings: Settings) -> None:
    """``WEB_SET_MENU_BUTTON=false`` — hech narsa qilinmaydi."""
    settings.web.set_menu_button = False
    settings.web.web_app_url = "https://77-42-77-60.sslip.io/"
    bot = StubBot()

    assert await ensure_menu_button(bot, settings) is False
    assert bot.calls == []


async def test_telegram_error_does_not_break_startup(settings: Settings) -> None:
    """Telegram xatosi startup'ni o'ldirmaydi — ``False`` qaytadi, exception chiqmaydi."""
    settings.web.set_menu_button = True
    settings.web.web_app_url = "https://77-42-77-60.sslip.io/"
    bot = StubBot(fail=RuntimeError("Telegram said no"))

    assert await ensure_menu_button(bot, settings) is False


async def test_button_text_is_clamped_to_64_chars(settings: Settings) -> None:
    """Telegram cheklovi: menyu matni 64 belgidan oshmasligi kerak."""
    settings.web.set_menu_button = True
    settings.web.web_app_url = "https://77-42-77-60.sslip.io/"
    settings.web.menu_button_text = "B" * 200
    bot = StubBot()

    assert await ensure_menu_button(bot, settings) is True
    assert len(bot.calls[0]["menu_button"].text) == 64


async def test_trailing_whitespace_is_stripped(settings: Settings) -> None:
    """``.env`` dagi tasodifiy bo'shliqlar manzilni buzmaydi."""
    settings.web.set_menu_button = True
    settings.web.web_app_url = "  https://77-42-77-60.sslip.io/  "
    bot = StubBot()

    assert await ensure_menu_button(bot, settings) is True
    assert bot.calls[0]["menu_button"].web_app.url == "https://77-42-77-60.sslip.io/"


def test_defaults_enable_menu_button() -> None:
    """Sukut: yoqilgan va matn ``☁️ Bulut``."""
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.web.set_menu_button is True
    assert settings.web.menu_button_text == "\u2601\ufe0f Bulut"
    get_settings.cache_clear()


def test_main_calls_ensure_menu_button_after_commands() -> None:
    """``main.py`` startup oqimida funksiya chaqiriladi (tartib muhim)."""
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "ensure_menu_button," in source, "import qilinmagan"
    assert "await ensure_menu_button(bot, settings)" in source
    assert source.index("await register_commands(bot)") < source.index(
        "await ensure_menu_button(bot, settings)"
    )


def test_web_app_url_derives_from_base_url() -> None:
    """``WEB_APP_URL`` bo'sh bo'lsa ``BASE_URL + WEB_MOUNT_PATH`` dan yasaladi."""
    get_settings.cache_clear()
    settings = get_settings()
    expected = f"{settings.app.base_url}{settings.web.mount_path}".rstrip("/")
    assert (settings.web.web_app_url or "").rstrip("/") == expected
    get_settings.cache_clear()
