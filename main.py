#!/usr/bin/env python3
"""Ishga tushirish nuqtasi: bot + API bitta jarayonda.

Nima uchun bitta jarayon?
-------------------------
Telegram'ning bitta bot tokeni uchun **faqat bitta** faol ulanish bo'lishi
mumkin: yoki long polling, yoki webhook. Render/Heroku kabi PaaS'lar esa HTTP
portni talab qiladi. Shuning uchun bu yerda:

* ``--mode all``  (sukut) — FastAPI ``PORT`` da ishlaydi **va** bot polling qiladi
  (bitta event loop, bitta DB pool, bitta kesh);
* ``--mode bot``  — faqat bot (worker konteyner uchun);
* ``--mode api``  — faqat HTTP (webhook rejimida yoki Mini App uchun);
* ``--mode migrate`` — sxemani yangilash (:mod:`scripts.migrate`) va chiqish.

Ishga tushish tartibi:

1. logging (``LOG_LEVEL``);
2. til o'rnatish (``APP_LANGUAGE=uz`` -> :func:`bot.texts_hugecloud.install_language`);
3. DB: ``init_db`` + yetishmayotgan ustunlar tekshiruvi (migratsiya eslatmasi);
4. ``system_flags`` dan jonli sozlamalar (``/diyor`` o'zgartirganlari);
5. bot + dispatcher + arxiv vaulti;
6. BotFather buyruqlari ro'yxati (``/files``, ``/login``, ``/diyor`` …);
7. webhook yoki polling;
8. API (ixtiyoriy) — ``0.0.0.0:PORT`` da.

O'chirish: polling to'xtatiladi, bot sessiyasi, HTTP klient, kesh, rate limiter
va DB pool yopiladi (SIGINT/SIGTERM signal orqali).
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import logging
import os
import sys
from collections.abc import Sequence
from typing import Annotated, Any

# Webhook marshruti uchun FastAPI turlari. **Modul darajasida** import qilinishi
# shart: ``from __future__ import annotations`` tufayli annotatsiyalar satr
# bo'lib qoladi va FastAPI ularni modul ``globals()`` dan qidiradi — funksiya
# ichidagi lokal import bu rezolyutsiyani buzadi (``Request`` query param deb
# talqin qilinadi).
from fastapi import Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response

# ``python main.py`` to'g'ridan-to'g'ri ishga tushganda paketni topish uchun.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Settings, get_settings
from db.database import dispose_engine, init_db

logger = logging.getLogger("hugecloud")

#: Ishga tushirish rejimlari.
MODES = ("all", "bot", "api", "migrate")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging(settings: Settings) -> None:
    """Structured-ish console logging (PaaS log drayverlari uchun qulay)."""
    level = getattr(logging, settings.app.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Uchinchi tomon kutubxonalarining shovqinini kamaytirish.
    for noisy in ("aiogram.event", "httpx", "httpcore", "asyncio", "aiosqlite"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


#: Matnlar bir marta o'rnatiladi (``main`` va ``amain`` ikkalasi ham chaqirishi mumkin).
_language_installed: bool = False


def install_language(settings: Settings, *, force: bool = False) -> None:
    """``APP_LANGUAGE`` ga qarab **barcha** UI matnlarini o'rnatish.

    Nega bu qadam majburiy: ``bot.texts`` moduli dinamik to'ldiriladi —
    ``install_language`` chaqirilmaguncha unda ``auth_required_action`` kabi
    funksiyalar *mavjud emas* va bot birinchi xabarda ``AttributeError`` bilan
    yiqiladi. Shu sababli bu chaqiruv ``main()`` (sinxron) va ``amain()``
    (async) ichida ham bor va :func:`ensure_language` orqali idempotent.
    """
    global _language_installed
    if _language_installed and not force:
        return

    from bot.texts_hugecloud import install_language as install

    report = install(settings.app.language)
    _language_installed = True
    logger.info(
        "Language %s installed (legacy=%d, hugecloud=%d strings)",
        settings.app.language,
        report.get("legacy", 0),
        report.get("hugecloud", 0),
    )


def ensure_language(settings: Settings) -> bool:
    """Matnlar o'rnatilmagan bo'lsa o'rnatish; ``True`` = endi o'rnatildi.

    Bu "sug'urta" qatlami: ``amain`` ni to'g'ridan-to'g'ri (``main()`` orqali
    emas) chaqirgan integratsion skriptlar ham matnlarni tayyor holda oladi.
    """
    if _language_installed:
        return False
    install_language(settings)
    return True


# ---------------------------------------------------------------------------
# Schema check (migratsiya qilinmagan bo'lsa — aniq xabar)
# ---------------------------------------------------------------------------
async def verify_schema(*, auto_migrate: bool = True) -> None:
    """Yetishmayotgan ustunlarni tekshirish va (ixtiyoriy) avtomatik qo'shish.

    ``create_all`` yangi **jadvallarni** yaratadi, lekin mavjud jadvalga ustun
    qo'shmaydi. Shuning uchun bu yerda :data:`scripts.migrate.COLUMN_PLAN`
    bo'yicha tekshiramiz: eski bazada yangi ustunlar yo'q bo'lsa, migratsiya
    chaqiriladi (ma'lumotlar o'chmaydi — faqat ``ALTER TABLE ADD COLUMN``).
    """
    from db.database import get_engine
    from scripts.migrate import COLUMN_PLAN, existing_columns, existing_tables, migrate

    engine = get_engine()
    try:
        tables = await existing_tables(engine)
        missing: list[str] = []
        for table, plan in COLUMN_PLAN.items():
            if table not in tables:
                continue
            present = await existing_columns(engine, table)
            missing.extend(f"{table}.{column}" for column, _ in plan if column not in present)

        if not missing:
            logger.info("Schema is up to date (%d tables)", len(tables))
            return

        logger.warning("Schema is missing %d column(s): %s", len(missing), ", ".join(missing[:12]))
        if not auto_migrate:
            logger.error("Run: python scripts/migrate.py   (data is preserved)")
            return

        logger.info("Auto-migrating (ALTER TABLE ADD COLUMN only, no data loss)…")
        summary = await migrate(str(engine.url))
        logger.info(
            "Migration complete: +%d tables, +%d columns",
            len(summary["tables_created"]),
            len(summary["columns_added"]),
        )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------
#: Polling qayta urinishlari orasidagi pauza (soniya).
POLLING_BACKOFF: tuple[float, ...] = (2.0, 5.0, 10.0, 30.0)


async def run_polling(settings: Settings, dispatcher: Any, bot: Any) -> None:
    """Long polling ni ishga tushirish — xatoda **qayta urinadi**.

    Nega qayta urinish kerak: ``RUN_MODE=all`` da bot va API bitta jarayonda
    ishlaydi. Agar polling bitta tarmoq uzilishi yoki noto'g'ri token tufayli
    yiqilsa, butun jarayonni (shu jumladan HTTP API va Mini App'ni) o'chirish
    noto'g'ri bo'lardi. Shuning uchun orqaga chekinish bilan qayta urinamiz;
    faqat ``CancelledError`` (to'xtatish signali) darhol yuqoriga uzatiladi.
    """
    logger.info(
        "Starting polling for @%s (drop_pending=%s)",
        settings.bot.username,
        settings.bot.polling_drop_pending,
    )
    # ``resolve_used_update_types()`` faqat router'larda ro'yxatdan o'tgan
    # update turlarini qaytaradi (ortiqcha trafik yo'q).
    allowed_updates = dispatcher.resolve_used_update_types()
    attempt = 0
    while True:
        try:
            await dispatcher.start_polling(
                bot,
                allowed_updates=allowed_updates,
                drop_pending_updates=settings.bot.polling_drop_pending,
                polling_timeout=settings.bot.polling_timeout,
                handle_signals=False,  # signallarni o'zimiz boshqaramiz
            )
            logger.info("Polling finished cleanly")
            return
        except asyncio.CancelledError:
            # To'xtatish so'rovi — qayta urinishsiz chiqamiz.
            logger.info("Polling cancelled")
            raise
        except Exception as exc:
            delay = POLLING_BACKOFF[min(attempt, len(POLLING_BACKOFF) - 1)]
            attempt += 1
            logger.error(
                "Polling failed (%s). Retrying in %.0fs (attempt #%d); API keeps serving.",
                exc,
                delay,
                attempt,
            )
            await asyncio.sleep(delay)


async def _set_webhook_once(settings: Settings, dispatcher: Any, bot: Any) -> str:
    """Bitta ``setWebhook`` chaqiruvi (xato bo'lsa istisno ko'taradi)."""
    if not settings.api.webhook_url:
        raise ValueError("WEBHOOK_URL is empty but WEBHOOK_ENABLED=true")
    webhook_url = settings.api.webhook_url.rstrip("/") + settings.api.webhook_path
    await bot.set_webhook(
        url=webhook_url,
        secret_token=settings.api.webhook_secret or None,
        drop_pending_updates=settings.bot.polling_drop_pending,
        allowed_updates=dispatcher.resolve_used_update_types(),
    )
    return webhook_url


#: Webhook ro'yxatdan o'tkazish uchun qayta urinishlar orasidagi pauza.
WEBHOOK_BACKOFF: tuple[float, ...] = (5.0, 15.0, 30.0, 60.0)


async def register_webhook(
    settings: Settings, dispatcher: Any, bot: Any, *, max_attempts: int | None = None
) -> None:
    """Telegram'ga webhook manzilini xabar qilish — **qayta urinishlar bilan**.

    Nega shunchaki ``await`` emas: ``WEBHOOK_URL`` noto'g'ri bo'lsa yoki Telegram
    vaqtincha javob bermasa, istisno butun ``amain`` ni yiqitardi va API ham
    o'chib qolardi (garchi HTTP server allaqachon tayyor bo'lsa ham).

    Shuning uchun:

    * konfiguratsiya xatosi (``WEBHOOK_URL`` bo'sh) — darhol, aniq xato;
    * tarmoq xatosi — ``5s → 15s → 30s → 60s`` pauza bilan qayta urinish,
      API bu vaqtda xizmat ko'rsatishda davom etadi (Telegram update yubora
      olmaydi, lekin Mini App va REST ishlaydi).
    """
    if not settings.api.webhook_url:
        # Konfiguratsiya xatosi — qayta urinish ma'nosiz, darhol to'xtatamiz.
        raise ValueError("WEBHOOK_URL is empty but WEBHOOK_ENABLED=true")

    attempts = 0
    while True:
        attempts += 1
        try:
            webhook_url = await _set_webhook_once(settings, dispatcher, bot)
            logger.info("Telegram webhook set to %s", webhook_url)
            return
        except asyncio.CancelledError:
            logger.info("Webhook registration cancelled")
            raise
        except ValueError:
            # Konfiguratsiya xatosi: qayta urinish natija bermaydi.
            raise
        except Exception as exc:
            if max_attempts is not None and attempts >= max_attempts:
                logger.error("Webhook registration failed after %d attempts: %s", attempts, exc)
                return
            delay = WEBHOOK_BACKOFF[min(attempts - 1, len(WEBHOOK_BACKOFF) - 1)]
            logger.error(
                "setWebhook failed (%s). Retrying in %.0fs (attempt #%d); API keeps serving.",
                exc,
                delay,
                attempts,
            )
            await asyncio.sleep(delay)


def attach_webhook(application: Any, dispatcher: Any, bot: Any, settings: Settings) -> None:
    """Webhook marshrutini **mavjud FastAPI ilovasiga** o'rnatish.

    Nega alohida aiohttp server emas: ``RUN_MODE=all`` da API allaqachon
    ``settings.api.port`` ni band qilgan. Ikkinchi server shu portni yana
    band qilmoqchi bo'lsa ``address already in use`` bilan yiqilardi. Bitta
    port = bitta HTTP server = PaaS (Render/Fly) talabiga mos.

    Xavfsizlik: ``WEBHOOK_SECRET`` berilgan bo'lsa, Telegram har so'rovda
    ``X-Telegram-Bot-Api-Secret-Token`` sarlavhasini yuboradi va biz uni
    ``hmac.compare_digest`` (vaqt bo'yicha bir xil taqqoslash) bilan tekshiramiz.
    """
    from aiogram.types import Update

    secret = settings.api.webhook_secret
    path = settings.api.webhook_path
    started = False

    async def _start_dispatcher() -> None:
        """``startup`` hodisasini bir marta yuborish (middleware'lar shu yerda tayyorlanadi)."""
        nonlocal started
        if started:
            return
        started = True
        try:
            await dispatcher.emit_startup(bot=bot)
            logger.info("Dispatcher startup emitted (webhook mode)")
        except Exception:
            logger.exception("dispatcher.emit_startup failed")

    async def _stop_dispatcher() -> None:
        nonlocal started
        if not started:
            return
        started = False
        try:
            await dispatcher.emit_shutdown(bot=bot)
        except Exception:
            logger.debug("dispatcher.emit_shutdown failed", exc_info=True)

    @application.post(path, include_in_schema=False)
    async def telegram_webhook(
        request: Request,
        x_telegram_bot_api_secret_token: Annotated[str | None, Header(alias="X-Telegram-Bot-Api-Secret-Token")] = None,
    ) -> Response:
        """Telegram yuborgan ``Update`` ni qabul qilib dispetcherga uzatish."""
        if secret and not hmac.compare_digest(
            x_telegram_bot_api_secret_token or "", secret
        ):
            # Manba noto'g'ri — hech qanday ma'lumotni oshkor qilmaslik uchun 403.
            logger.warning("Webhook request rejected: bad secret token")
            raise HTTPException(status_code=403, detail="invalid secret token")

        try:
            payload = await request.json()
            update = Update.model_validate(payload, context={"bot": bot})
        except Exception as exc:
            logger.warning("Invalid webhook payload: %s", exc)
            raise HTTPException(status_code=400, detail="invalid update payload") from exc

        await _start_dispatcher()
        try:
            # ``feed_update`` natijani qaytarmaydi; javobni Telegram "OK" kutadi.
            await dispatcher.feed_update(bot=bot, update=update)
        except asyncio.CancelledError:  # pragma: no cover - graceful shutdown
            raise
        except Exception as exc:
            # MUHIM: handler/middleware xatosi uchun Telegram'ga 500 qaytarsak,
            # Telegram xuddi shu update'ni qayta-qayta yuboradi (retry storm) va
            # xato har safar takrorlanadi. aiogram polling rejimida ham xatoni
            # log qilib, update'ni "qayta ishlandi" deb hisoblaydi — shu
            # xatti-harakatni webhook'da ham takrorlaymiz.
            # ``logger.error`` (``exception`` emas): to'liq traceback allaqachon
            # ``ErrorLoggingMiddleware`` tomonidan log qilingan — bu yerda faqat
            # update identifikatori bilan qisqa xabar qoldiramiz.
            logger.error(
                "Returning 200 to Telegram despite failed update id=%s (%s: %s)",
                update.update_id,
                exc.__class__.__name__,
                exc,
            )
        return JSONResponse({"ok": True})

    @application.on_event("shutdown")
    async def _webhook_shutdown() -> None:  # pragma: no cover - shutdown yo'li
        await _stop_dispatcher()

    logger.info("Webhook route attached to FastAPI at %s", path)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
async def run_api(
    settings: Settings,
    *,
    dispatcher: Any | None = None,
    bot: Any | None = None,
) -> tuple[Any, Any]:
    """Uvicorn serverini tayyorlash: ``(server, app)``.

    ``dispatcher``/``bot`` berilsa va ``WEBHOOK_ENABLED=true`` bo'lsa, webhook
    marshruti **shu ilovaga** o'rnatiladi (alohida port band qilinmaydi).
    """
    import uvicorn

    from api.app import create_app

    app = create_app(settings)
    if dispatcher is not None and bot is not None and settings.api.webhook_enabled:
        attach_webhook(app, dispatcher, bot, settings)

    config = uvicorn.Config(
        app=app,
        host=settings.api.host,
        port=settings.api.port,
        log_level=settings.app.log_level.lower(),
        # DIQQAT: tunnel (cloudflared) nginx'ni chetlab o'tadi, shuning uchun
        # so'rovlar faqat shu jurnalda ko'rinadi. Ilgari access_log DEBUG ga
        # bog'langan edi va ishlab chiqarishda ko'r bo'lib qoldik
        # (2026-09-23 hodisasi: "ilova ochilmayapti" ni tekshirish qiyin bo'ldi).
        access_log=True,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    server = uvicorn.Server(config)
    # Signal'larni uvicorn emas, biz boshqaramiz (bot bilan bir jarayonda).
    server.config.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    logger.info("API listening on http://%s:%d (health: GET /healthz)", settings.api.host, settings.api.port)
    return server, app


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
async def amain(mode: str, settings: Settings) -> int:
    """Asosiy async oqim; jarayon yakunida ``0`` qaytaradi."""
    from services.cache import close_cache, init_cache
    from services.rate_limiter import close_rate_limiter, init_rate_limiter

    # Sug'urta: amain() to'g'ridan-to'g'ri chaqirilsa ham matnlar tayyor bo'lsin.
    ensure_language(settings)

    await init_db()
    await verify_schema(auto_migrate=settings.db.auto_migrate)
    await init_cache()
    await init_rate_limiter()

    bot = None
    dispatcher = None
    api_server = None
    tasks: list[asyncio.Task[Any]] = []

    try:
        if mode in {"all", "bot"}:
            from bot.setup import (
                apply_runtime_flags,
                build_bot,
                build_dispatcher,
                ensure_menu_button,
                install_vault,
                register_commands,
            )

            await apply_runtime_flags(settings)
            bot = build_bot(settings)
            install_vault(bot, settings)
            dispatcher = build_dispatcher(settings)
            await register_commands(bot)
            # Mini App manzili har restartda Telegram'ga yoziladi: tunnel/URL
            # o'zgarsa ham menyu tugmasi eskirib qolmaydi.
            await ensure_menu_button(bot, settings)
            logger.info("Bot ready: @%s", settings.bot.username)

        # API'ni botdan KEYIN ishga tushiramiz: webhook marshruti ilovaga
        # server tinglashdan oldin ulanishi kerak.
        if mode in {"all", "api"}:
            api_server, _api_app = await run_api(
                settings, dispatcher=dispatcher, bot=bot
            )
            tasks.append(asyncio.create_task(api_server.serve(), name="api"))

        if mode == "bot":
            await run_polling(settings, dispatcher, bot)
        elif mode == "all" and bot is not None and dispatcher is not None:
            if settings.api.webhook_enabled:
                # Webhook rejimida Telegram update'larni o'zi yuboradi —
                # polling ishlatilmaydi (aks holda 409 Conflict bo'ladi).
                await register_webhook(settings, dispatcher, bot)
            else:
                tasks.append(
                    asyncio.create_task(
                        run_polling(settings, dispatcher, bot), name="polling"
                    )
                )

        if not tasks:
            logger.error("Nothing to run for mode=%s", mode)
            return 1

        # Birinchi vazifa tugasa (xato yoki to'xtatish) — qolganlarini ham
        # yopamiz, aks holda jarayon "osilib" qoladi.
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.exception() is not None:
                logger.error("Task %s failed: %s", task.get_name(), task.exception())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        return 0

    finally:
        if dispatcher is not None:
            try:
                await dispatcher.stop_polling()
            except Exception:  # o'chirish paytida xato muhim emas (log qilinadi)
                logger.debug("stop_polling failed during shutdown", exc_info=True)
        if bot is not None:
            try:
                await bot.session.close()
            except Exception:  # o'chirish paytida xato muhim emas (log qilinadi)
                logger.debug("Bot session close failed", exc_info=True)
        await close_rate_limiter()
        await close_cache()
        await dispose_engine()
        logger.info("Shutdown complete")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """CLI argumentlari (``RUN_MODE`` env bilan ham boshqariladi)."""
    parser = argparse.ArgumentParser(description="hugecloud — Telegram Cloud Bot + API")
    parser.add_argument(
        "--mode",
        choices=MODES,
        default=os.getenv("RUN_MODE", "all"),
        help="all = bot + API (sukut), bot = faqat polling, api = faqat HTTP, migrate = sxemani yangilash",
    )
    parser.add_argument("--host", default=None, help="API host (API_HOST ni ustun bosadi)")
    parser.add_argument("--port", type=int, default=None, help="API port (PORT ni ustun bosadi)")
    parser.add_argument("--log-level", default=None, help="DEBUG | INFO | WARNING | ERROR")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Sync entry point: konfiguratsiya, logging va ``asyncio.run``."""
    args = parse_args(argv)

    # CLI qiymatlari env'dan ustun (PaaS'da ``PORT`` muhim).
    if args.port is not None:
        os.environ["PORT"] = str(args.port)
    if args.host:
        os.environ["API_HOST"] = args.host
    if args.log_level:
        os.environ["LOG_LEVEL"] = args.log_level

    settings = get_settings()
    setup_logging(settings)
    install_language(settings)  # UI matnlari (bot.texts) shu yerda to'ldiriladi

    banner = settings.describe()
    logger.info("Starting %s v%s (%s)", banner["bot_username"], settings.version, settings.app.env)
    logger.info("Config: %s", banner)

    if args.mode == "migrate":
        from scripts.migrate import main as migrate_main

        return migrate_main([])

    try:
        return asyncio.run(amain(args.mode, settings))
    except KeyboardInterrupt:  # pragma: no cover
        logger.info("Interrupted by user")
        return 0
    except Exception:
        logger.exception("Fatal error during startup/runtime")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
