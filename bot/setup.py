"""Bot & dispatcher factory.

Wires everything together in a deterministic order:

1. :class:`aiogram.Bot` (HTML parse mode, sane timeouts);
2. :class:`aiogram.Dispatcher` with memory FSM storage;
3. dispatcher-level middlewares — logging, **DB session + DI**, **throttling**;
4. router-level middleware — **auth** (needs a session, so it runs inside);
5. routers, in routing-priority order.

Middleware order matters: ``db_session`` is outermost so ``throttling`` and
``auth`` can already use the injected services.
"""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.middlewares.request_logging import RequestLogging
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.handlers import (
    admin_router,
    auth_router,
    browsing_router,
    callbacks_router,
    commands_router,
    files_router,
    inline_router,
    miniapp_router,
    share_router,
    storage_router,
    upload_router,
)
from bot.handlers.commands import register_start_router
from bot.middlewares.album import AlbumMiddleware
from bot.middlewares.auth import AuthMiddleware
from bot.middlewares.auth_gate import PasswordGateMiddleware
from bot.middlewares.auth_guard import AuthGuardMiddleware, set_auth_override
from bot.middlewares.db_session import DatabaseSessionMiddleware
from bot.middlewares.error_logging import ErrorLoggingMiddleware
from bot.middlewares.logging import UpdateLoggingMiddleware
from bot.middlewares.throttling import ThrottlingMiddleware
from bot.utils.emojis import load_custom_emoji_from_env
from config import Settings, get_settings
from db.models.flag import FLAG_ALLOW_REGISTER, FLAG_AUTH_REQUIRED, load_flags
from services.vault import ArchiveVault, set_vault

logger = logging.getLogger(__name__)

__all__ = [
    "apply_runtime_flags",
    "build_bot",
    "build_dispatcher",
    "ensure_menu_button",
    "install_vault",
    "register_commands",
    "register_middlewares",
    "register_routers",
    "setup_bot",
]


def build_bot(settings: Settings | None = None) -> Bot:
    """Create the aiogram ``Bot`` client from configuration.

    In debug mode every Bot API call is logged (``RequestLogging``). It is off in
    production on purpose: those logs contain file ids and chat ids, and they are
    noisy enough to hide real problems.
    """
    settings = settings or get_settings()
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(
            parse_mode=settings.bot.parse_mode or ParseMode.HTML,
            protect_content=False,
        ),
    )
    if settings.app.debug:
        bot.session.middleware(RequestLogging())
    return bot


def build_dispatcher(settings: Settings | None = None) -> Dispatcher:
    """Create the dispatcher, register middlewares and routers."""
    settings = settings or get_settings()
    dispatcher = Dispatcher(
        storage=MemoryStorage(),
        settings=settings,
        bot_username=settings.bot.username,
    )

    register_middlewares(dispatcher, settings)
    register_routers(dispatcher)
    return dispatcher


def register_middlewares(dispatcher: Dispatcher, settings: Settings | None = None) -> None:
    """Attach logging, DB/DI, throttling and auth middlewares (in that order)."""
    settings = settings or get_settings()

    # 0) Error net: eng tashqi qatlam — har qanday istisnoni kontekst bilan log
    #    qiladi (qaysi chat/buyruq) va qayta ko'taradi. Webhook qatlami uni
    #    ushlab Telegram'ga 200 qaytaradi, ya'ni "retry storm" bo'lmaydi.
    dispatcher.update.outer_middleware(ErrorLoggingMiddleware())

    # 1) Update logging (har bir update'ni ko'radi).
    dispatcher.update.outer_middleware(UpdateLoggingMiddleware())

    # 2) One DB session + all services per update.
    dispatcher.update.outer_middleware(DatabaseSessionMiddleware(settings))

    # 3) Throttling needs the limiter injected above, so it comes next.
    if settings.security.rate_limit_enabled:
        dispatcher.update.outer_middleware(ThrottlingMiddleware(settings))

    # 4) Auth runs on message/callback/inline only (it needs a session).
    auth = AuthMiddleware(settings)
    dispatcher.message.middleware(auth)
    dispatcher.callback_query.middleware(auth)
    dispatcher.inline_query.middleware(auth)

    # 5) 🔐 Legacy password gate (no-op when BOT_PASSWORD is empty). Registered
    #    after auth so it can read the persisted User row.
    if settings.bot.password_value():
        gate = PasswordGateMiddleware(settings)
        dispatcher.message.middleware(gate)
        dispatcher.callback_query.middleware(gate)
        logger.info("Password gate enabled (BOT_PASSWORD is set)")

    # 6) 🔐 hugecloud auth guard: hisob tasdiqlanmaguncha bulut funksiyalari
    #    ishlamaydi (faqat /start, /login, /register ochiq).
    guard = AuthGuardMiddleware(settings)
    dispatcher.message.middleware(guard)
    dispatcher.callback_query.middleware(guard)
    dispatcher.inline_query.middleware(guard)
    if settings.auth.required:
        logger.info("Auth guard enabled (AUTH_REQUIRED=true)")

    # 7) Media-group collecting so an album produces tidy confirmations.
    dispatcher.message.middleware(AlbumMiddleware())


def register_routers(dispatcher: Dispatcher) -> Router:
    """Include all routers; the order below *is* the routing priority.

    1.  ``callbacks`` - button presses (most specific traffic)
    2.  ``browsing``  - legacy category tabs, 📌 pin, move, delete, password gate
    3.  ``files``     - hugecloud ``/files``: 🎬 🎵 📄 📦 groups + 🔃 sorting
    4.  ``share``     - share manager callbacks + pass-code FSM
    5.  ``miniapp``   - 📱 ``web_app_data`` (Upload/Import) + Import FSM
    6.  ``admin``     - 🔐 owner panel ochiq parol bilan (callbacks + FSM)
    7.  ``auth``      - 🔐 ``/register``, ``/login``, ``/logout``, ``/password``
    8.  ``commands``  - slash commands, reply buttons, unknown text
    9.  ``storage``   - FSM text flows (folder/rename/move/search)
    10. ``upload``    - media intake, dedupe + archive vault
    11. ``inline``    - inline queries (``@bot query`` in any chat)
    12. ``start``     - ``/start`` + deep links + 🚪 gate, appended last so that
        button texts are matched before the generic text handler of ``commands``.
    """
    root = Router(name="root")
    root.include_router(callbacks_router)
    # Legacy browsing (category tabs, pin/move/delete, password gate) comes
    # before the generic callback router so its string callbacks win.
    root.include_router(browsing_router)
    root.include_router(files_router)
    root.include_router(share_router)
    # 📱 Mini App ko'prigi: ``web_app_data`` (Upload/Import tugmalari) va
    # ``ImportState`` dialogi. Auth/admin'dan OLDIN turadi — aks holda import
    # matnini boshqa FSM handler'lari tortib ketadi.
    root.include_router(miniapp_router)
    # Admin va auth routerlari FSM matn oqimlarini ushlaydi, shuning uchun
    # upload'ning "matn yubordingiz" handleridan OLDIN turishi shart.
    root.include_router(admin_router)
    root.include_router(auth_router)
    root.include_router(commands_router)
    root.include_router(storage_router)
    root.include_router(upload_router)
    root.include_router(inline_router)
    register_start_router(root)
    dispatcher.include_router(root)
    logger.info(
        "Routers registered: %s",
        ", ".join(router.name for router in root.sub_routers),
    )
    return root


def install_vault(bot: Bot, settings: Settings | None = None) -> ArchiveVault | None:
    """🛡️ Arxiv xizmatini yaratib, global singleton sifatida o'rnatish.

    ``ARCHIVE_CHANNEL_ID`` bo'sh yoki ``ARCHIVE_ENABLED=false`` bo'lsa xizmat
    baribir yaratiladi (holat tekshiruvi uchun), lekin ``enabled`` ``False``
    bo'ladi va yuklamalar nusxalanmaydi.
    """
    settings = settings or get_settings()
    vault = ArchiveVault(bot, settings=settings)
    set_vault(vault)
    if vault.enabled:
        logger.info("Archive vault enabled for channel %s", settings.archive.channel_id)
    else:
        logger.info("Archive vault disabled (ARCHIVE_CHANNEL_ID is empty or ARCHIVE_ENABLED=false)")
    return vault


async def apply_runtime_flags(settings: Settings | None = None) -> dict[str, str]:
    """⚙️ ``system_flags`` jadvalidagi jonli sozlamalarni qo'llash.

    Owner paneli autentifikatsiyani qayta ishga tushirmasdan o'zgartiradi;
    qiymat shu jadvalda saqlanadi va startup'da qayta o'qiladi. Jadval hali
    mavjud bo'lmasa (migratsiya qilinmagan) — sukut qiymatlar qoladi.
    """
    from db.database import get_session_factory

    settings = settings or get_settings()
    try:
        factory = get_session_factory()
        async with factory() as session:
            flags = await load_flags(session)
    except Exception as exc:
        logger.info("Runtime flags not loaded (%s); using .env values", exc)
        return {}

    if FLAG_AUTH_REQUIRED in flags:
        value = flags[FLAG_AUTH_REQUIRED].strip().lower() in {"1", "true", "on", "yes"}
        settings.auth.required = value
        set_auth_override(value)
    if FLAG_ALLOW_REGISTER in flags:
        settings.auth.allow_registration = (
            flags[FLAG_ALLOW_REGISTER].strip().lower() in {"1", "true", "on", "yes"}
        )
    if flags:
        logger.info("Runtime flags applied: %s", ", ".join(sorted(flags)))
    return flags


#: ``/setcommands`` uchun ham, ``set_my_commands`` uchun ham bir xil ro'yxat.
BOT_COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "Bosh menyu"),
    ("files", "Fayllar: guruhlar va saralash"),
    ("storage", "Bulut brauzeri (papkalar)"),
    ("sort", "Saralash tartibini tanlash"),
    ("groups", "Fayl guruhlari"),
    ("search", "Fayl qidirish"),
    ("stats", "Statistika"),
    ("share", "Ulashish havolalari"),
    ("settings", "Sozlamalar"),
    ("register", "Hisob ochish: /register login parol"),
    ("login", "Tizimga kirish: /login login parol"),
    ("logout", "Tizimdan chiqish"),
    ("profiles", "Profillar: ro'yxat va almashtirish"),
    ("password", "Parolni almashtirish"),
    ("id", "Telegram ID ni ko'rish"),
    ("cancel", "Joriy amalni bekor qilish"),
    ("help", "Yordam"),
)


async def register_commands(bot: Bot) -> None:
    """BotFather ro'yxatini yangilash (menyuda buyruqlar ko'rinishi uchun)."""
    from aiogram.types import BotCommand, BotCommandScopeDefault

    try:
        await bot.set_my_commands(
            [BotCommand(command=name, description=text) for name, text in BOT_COMMANDS],
            scope=BotCommandScopeDefault(),
        )
        logger.info("Bot commands registered (%d)", len(BOT_COMMANDS))
    except Exception as exc:
        logger.warning("Could not register bot commands: %s", exc)


async def ensure_menu_button(bot: Bot, settings: Settings | None = None) -> bool:
    """Asosiy menyu tugmasini (Main Mini App) joriy ``WEB_APP_URL`` ga o'rnatadi.

    Nega har startup'da? Tunnel skripti (``scripts/tunnel.sh``) ``.env`` dagi
    ``BASE_URL`` ni yangilardi, lekin ``setChatMenuButton`` ni **chaqirmasdi** —
    natijada Telegram'dagi menyu tugmasi eski/o'lik manzilga ishora qilib
    qolardi va Mini App ochilmasdi. Bu funksiya idempotent: har ishga tushishda
    Telegram'ga joriy manzil yoziladi, shuning uchun restart yetarli.

    Xatolar startup'ni o'ldirmaydi (webhook qattiqligidagi kabi yondashuv):
    tarmoq muammosi bo'lsa ogohlantirish log'lanadi va bot ishlashda davom etadi.

    Telegram talablari: manzil **HTTPS** va amal qiladigan sertifikat bilan.
    ``http://localhost`` ishga tushirishda rad etiladi (faqat ogohlantirish).

    Returns:
        ``True`` — tugma o'rnatildi; ``False`` — o'rnatilmadi (o'chirilgan,
        manzil HTTPS emas yoki Telegram xato qaytardi).
    """
    settings = settings or get_settings()
    if not settings.web.set_menu_button:
        logger.info("Menu button skipped (WEB_SET_MENU_BUTTON=false)")
        return False

    url = (settings.web.web_app_url or "").strip()
    if not url.startswith("https://"):
        logger.warning(
            "Menu button o'rnatilmadi: WEB_APP_URL=%s — Telegram faqat HTTPS va "
            "amal qiladigan sertifikatli manzilni qabul qiladi.",
            url or "<bo'sh>",
        )
        return False

    from aiogram.types import MenuButtonWebApp, WebAppInfo

    text = (settings.web.menu_button_text or "☁️ Bulut").strip()[:64]
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text=text, web_app=WebAppInfo(url=url))
        )
    except Exception as exc:
        logger.warning("Could not set menu button (%s): %s", url, exc)
        return False

    logger.info("Main Mini App menu button set: %s (%s)", url, text)
    return True


def setup_bot(settings: Settings | None = None) -> tuple[Bot, Dispatcher]:
    """Convenience entry point used by :mod:`main`."""
    settings = settings or get_settings()
    load_custom_emoji_from_env()
    bot = build_bot(settings)
    install_vault(bot, settings)
    dispatcher = build_dispatcher(settings)
    return bot, dispatcher
