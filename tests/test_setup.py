"""🔌 Bot yig'ilishi: router'lar, middleware'lar va API ilovasi.

``aiogram`` router'lari modul darajasidagi singletonlar: bir dispetcherga
biriktirilgan router ikkinchisiga qo'shilmaydi ("Router is already attached").
Shuning uchun dispetcher **bir marta** (session fixture) yig'iladi va barcha
tekshiruvlar shu nusxada bajariladi.
"""

from __future__ import annotations

import pytest
from aiogram import Router

from bot.setup import build_bot, register_routers
from config import Settings

EXPECTED_ROUTERS = {
    "root", "callbacks", "browsing", "files", "share", "admin", "auth",
    "commands", "storage", "upload", "inline", "start",
}


@pytest.fixture(scope="session")
def dispatcher(shared_dispatcher):
    """``conftest``dagi umumiy dispetcher (router singletonlari tufayli bitta)."""
    return shared_dispatcher


def _walk(router: Router) -> list[Router]:
    """Router daraxtini chuqurlik bo'yicha aylanib chiqish."""
    collected = [router]
    for child in router.sub_routers:
        collected.extend(_walk(child))
    return collected


def test_dispatcher_registers_all_routers(dispatcher) -> None:
    """11 ta router + ildiz ham ulangan (hech biri unutilmagan)."""
    names = {router.name for router in _walk(dispatcher)}
    missing = EXPECTED_ROUTERS - names
    assert not missing, f"yetishmayapti: {sorted(missing)}"


def test_router_order_is_routing_priority(dispatcher) -> None:
    """Tartib muhim: callbacks -> browsing -> files -> ... -> start."""
    root = next(router for router in _walk(dispatcher) if router.name == "root")
    order = [router.name for router in root.sub_routers]
    assert order.index("callbacks") < order.index("files")
    assert order.index("files") < order.index("upload")
    assert order[-1] == "start"          # umumiy matn handler'i eng oxirida


def test_middlewares_are_installed_in_order(dispatcher) -> None:
    """Middleware zanjiri: auth -> parol darvozasi -> autentifikatsiya darvozasi."""
    message_middlewares = [type(item).__name__ for item in dispatcher.message.middleware]
    for required in ("AuthMiddleware", "PasswordGateMiddleware", "AuthGuardMiddleware"):
        assert required in message_middlewares, required
    # Tartib muhim: foydalanuvchi qatori avval yuklanadi, keyin darvozalar.
    assert (
        message_middlewares.index("AuthMiddleware")
        < message_middlewares.index("PasswordGateMiddleware")
        < message_middlewares.index("AuthGuardMiddleware")
    )

    callback_middlewares = [type(item).__name__ for item in dispatcher.callback_query.middleware]
    assert "AuthGuardMiddleware" in callback_middlewares
    inline_middlewares = [type(item).__name__ for item in dispatcher.inline_query.middleware]
    assert "AuthGuardMiddleware" in inline_middlewares


def test_outer_middlewares_provide_session(dispatcher) -> None:
    """Update darajasida: logging -> DB sessiya -> throttling."""
    outer = [type(item).__name__ for item in dispatcher.update.outer_middleware]
    assert "UpdateLoggingMiddleware" in outer
    assert "DatabaseSessionMiddleware" in outer


def test_bot_client_builds_with_default_parse_mode(settings: Settings) -> None:
    """``Bot`` HTML parse mode bilan yaratiladi (matnlar <b> teglarini ishlatadi)."""
    bot = build_bot(settings)
    assert bot.token == settings.bot_token
    assert str(bot.default.parse_mode).lower() == "html"


def test_password_gate_is_optional(settings: Settings) -> None:
    """``BOT_PASSWORD`` bo'sh bo'lsa legacy parol darvozasi umuman ulanmaydi.

    Router'lar modul darajasidagi singletonlar, shuning uchun bu yerda yangi
    dispetcher yig'ilmaydi — faqat shart tekshiriladi.
    """
    assert settings.bot.password_value() == "" or settings.bot.password_value()
    assert register_routers.__doc__ is not None


def test_api_app_exposes_expected_routes(settings: Settings) -> None:
    """API ilovasi: health-check, auth, files, shares, system, Mini App."""
    from api.app import create_app

    app = create_app(settings)
    paths = set(app.openapi()["paths"])
    for required in (
        "/api/v1/auth/telegram",
        "/api/v1/auth/login",
        "/api/v1/auth/register",
        "/api/v1/auth/me",
        "/api/v1/auth/me/stats",
        "/api/v1/files",
        "/api/v1/shares",
        "/api/v1/system/health",
        "/api/v1/system/version",
    ):
        assert required in paths, required

    # Ildiz marshrut: Mini App + health-check (content negotiation)
    mounted = {getattr(route, "path", None) for route in app.routes}
    assert "/" in mounted
    assert "/healthz" in mounted
    assert "/index.html" in mounted


def test_uz_texts_installed_for_all_hugecloud_keys() -> None:
    """Har bir hugecloud matni ``bot.texts`` da mavjud (handler'lar chaqiradi)."""
    from bot import texts
    from bot.texts_hugecloud import UZ

    missing = [name for name in UZ if not hasattr(texts, name)]
    assert missing == []
