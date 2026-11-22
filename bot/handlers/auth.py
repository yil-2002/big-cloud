"""🔐 Autentifikatsiya handlerlari: ``/register``, ``/login``, ``/logout``, ``/password``.

Bu router :class:`~bot.middlewares.auth_guard.AuthGuardMiddleware` ruxsat bergan
buyruqlarni qayta ishlaydi, shuning uchun u **har doim** ochiq: hisobi yo'q
foydalanuvchi ham shu yerdan kiradi.

Ikki xil kiritish qo'llab-quvvatlanadi:

1. **Bir qatorli**: ``/register alisher parol123`` yoki ``/login alisher parol123``
   — argumentlar to'g'ridan-to'g'ri buyruq ichida;
2. **Ikki qadamli FSM**: argumentlar yetishmasa bot avval loginni, keyin parolni
   so'raydi (:class:`~bot.states.AuthState`). Parol kiritilayotganda xabar
   **o'chirib tashlanadi** ( Telegram'da parol tarixda qolmasligi uchun).

Barcha mantiq :class:`services.auth.AuthService` da: urinishlar hisoblagichi,
vaqtinchalik qulf, audit yozuvlari. Bu fayl faqat matn va klaviatura bilan
ishlaydi.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.filters.logic import or_f
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot import texts
from bot.keyboards.reply import (
    BTN_LOGIN,
    BTN_REGISTER,
    auth_reply_keyboard,
    cancel_reply_keyboard,
    main_reply_keyboard,
    text_of,
)
from bot.states import AuthState
from bot.utils.emojis import Emoji
from config import Settings
from db.models import User
from services.auth import AuthService
from services.share import ShareService
from services.storage import StorageService
from services.telegram import TelegramStorage

logger = logging.getLogger(__name__)

router = Router(name="auth")

#: Parol so'ralayotganda javob klaviaturasi.
_CANCEL_TEXTS = ("Cancel", "Bekor qilish", "Отмена", text_of(Emoji.CLOSE, "Cancel"))


def _split_credentials(args: str | None) -> tuple[str, str]:
    """``"alisher parol123"`` -> ``("alisher", "parol123")``.

    Parolda bo'shliq bo'lishi mumkin emas (``AUTH_*`` qoidalari shuni talab
    qiladi), shuning uchun ikkidan ortiq qism bo'lsa qolganlari parolga
    qo'shiladi — bu foydalanuvchi xatosini kamaytiradi.
    """
    cleaned = (args or "").strip()
    if not cleaned:
        return "", ""
    parts = cleaned.split(maxsplit=1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1].strip()


async def _delete_silently(message: Message) -> None:
    """Parolni o'z ichiga olgan xabarni chat tarixidan o'chirish."""
    try:
        await message.delete()
    except Exception:
        logger.debug("Could not delete the password message %s", message.message_id)


def _caller_id(message: Message, user: User) -> int:
    """Chaqiruvchining HAQIQIY Telegram ID si.

    Ko'p profilli rejimda ``user`` — aktiv profil (qo'shimcha profilning ID si
    manfiy sintetik son). Login/register esa Telegram foydalanuvchisi nomidan
    bajariladi: aynan u yangi profilga o'tadi.
    """
    return message.from_user.id if message.from_user else user.telegram_id


def _service(session, storage: StorageService, settings: Settings) -> AuthService:
    """AuthService ni mavjud sessiya/storage bilan qurish."""
    return AuthService(session, storage=storage, settings=settings)


# ---------------------------------------------------------------------------
# /register
# ---------------------------------------------------------------------------
async def _finish_auth_success(
    message: Message,
    user: User,
    storage: StorageService,
    share: ShareService,
    tg_storage: TelegramStorage,
    settings: Settings,
    state: FSMContext,
    *,
    login: str,
    flow: str,
) -> None:
    """Login/register muvaffaqiyatli tugadi: tasdiq + **uzluksiz davom**.

    Ikki qadam (product spec: deep-link seamless, Mini App pivot):

    1. qisqa tasdiq xabari (``login_ok`` / ``register_ok``);
    2. :func:`bot.handlers.start.resume_after_auth` — park qilingan deep-link
       bo'lsa uni ijro etadi, aks holda Mini App bosh ekranini ko'rsatadi.

    FSM holati :func:`resume_after_auth` ichida **tozalanmaydi**: park kaliti
    ``take_parked_payload`` tomonidan bo'shatiladi, holat esa ``/start`` yoki
    keyingi dialog o'zgartiradi. Shu sababli ``state.clear()`` ni bu yerda
    chaqirmaymiz — aks holda deep-link yo'qolardi.
    """
    from bot.handlers.start import resume_after_auth

    await message.answer(
        
            texts.login_ok(user.login or login, user.display_name)
            if flow == "login"
            else texts.register_ok(user.login or login)
        
    )
    await resume_after_auth(message, user, storage, share, tg_storage, settings, state)


@router.message(or_f(Command("register"), F.text.in_((BTN_REGISTER, text_of(Emoji.SPARKLES, BTN_REGISTER)))))
async def cmd_register(
    message: Message,
    user: User,
    storage: StorageService,
    share: ShareService,
    tg_storage: TelegramStorage,
    settings: Settings,
    state: FSMContext,
    # ``command`` ixtiyoriy: handler ``F.text.in_`` (reply tugma) orqali ham
    # chaqiriladi — u yo'lda aiogram ``CommandObject`` bermaydi. Majburiy
    # qilinsa, tugma bosilganda ``TypeError`` bo'lardi.
    command: CommandObject | None = None,
) -> None:
    """``/register <login> <parol>`` — yangi hisob ochish."""
    service = _service(storage.session, storage, settings)
    login, password = _split_credentials(command.args if command else None)

    if not login:
        # 1-qadam: loginni so'rash.
        await state.set_state(AuthState.waiting_login)
        await state.update_data(flow="register")
        await message.answer(
            texts.register_prompt(), reply_markup=cancel_reply_keyboard(user)
        )
        return

    if not password:
        await state.set_state(AuthState.waiting_password)
        await state.update_data(flow="register", login=login)
        await message.answer(
            f"{Emoji.KEY.plain()} <b>{login}</b> uchun parol kiriting "
            f"(kamida {settings.auth.min_password_length} belgi):",
            reply_markup=cancel_reply_keyboard(user),
        )
        return

    result = await service.register(_caller_id(message, user), login, password)
    if result.ok and result.user is not None:
        await _finish_auth_success(
            message,
            result.user,
            storage,
            share,
            tg_storage,
            settings,
            state,
            login=login,
            flow="register",
        )
        return

    await message.answer(
        result.message or texts.error_generic(), reply_markup=auth_reply_keyboard(user)
    )


@router.message(AuthState.waiting_login, F.text)
async def on_login_input(
    message: Message,
    user: User,
    storage: StorageService,
    settings: Settings,
    state: FSMContext,
) -> None:
    """FSM: login qabul qilindi — endi parolni so'raymiz."""
    value = (message.text or "").strip()
    if value in _CANCEL_TEXTS or value.startswith("/"):
        await state.clear()
        await message.answer(
            texts.operation_cancelled(),
            reply_markup=main_reply_keyboard(user, web_app_url=settings.web.web_app_url),
        )
        return

    data = await state.get_data()
    flow = data.get("flow", "register")

    if flow == "login":
        # /login oqimi: parolni so'rashga o'tamiz.
        await state.set_state(AuthState.waiting_password)
        await state.update_data(login=value)
        await message.answer(
            f"{Emoji.KEY.plain()} <b>{value}</b> parolini kiriting:",
            reply_markup=cancel_reply_keyboard(user),
        )
        await _delete_silently(message)
        return

    # /register oqimi: login nomini tekshiramiz.
    from services.auth import validate_login

    error = validate_login(value)
    if error:
        await message.answer(error, reply_markup=cancel_reply_keyboard(user))
        return
    if await storage.is_login_taken(value, exclude_telegram_id=user.telegram_id):
        await message.answer(texts.register_taken(value), reply_markup=cancel_reply_keyboard(user))
        return

    await state.set_state(AuthState.waiting_password)
    await state.update_data(login=value)
    await message.answer(
        f"{Emoji.KEY.plain()} <b>{value}</b> uchun parol o'ylab toping "
        f"(kamida {settings.auth.min_password_length} belgi):",
        reply_markup=cancel_reply_keyboard(user),
    )


@router.message(AuthState.waiting_password, F.text)
async def on_password_input(
    message: Message,
    user: User,
    storage: StorageService,
    share: ShareService,
    tg_storage: TelegramStorage,
    settings: Settings,
    state: FSMContext,
) -> None:
    """FSM: parol qabul qilindi — hisob yaratamiz yoki tizimga kiramiz."""
    password = (message.text or "").strip()
    data = await state.get_data()
    flow = data.get("flow", "register")
    login = str(data.get("login") or "")
    await _delete_silently(message)

    if password in _CANCEL_TEXTS or password.startswith("/"):
        await state.clear()
        await message.answer(
            texts.operation_cancelled(),
            reply_markup=main_reply_keyboard(user, web_app_url=settings.web.web_app_url),
        )
        return

    service = _service(storage.session, storage, settings)

    if flow == "login":
        result = await service.login_telegram(_caller_id(message, user), login, password)
    else:
        result = await service.register(_caller_id(message, user), login, password)

    if result.ok and result.user is not None:
        await _finish_auth_success(
            message,
            result.user,
            storage,
            share,
            tg_storage,
            settings,
            state,
            login=login,
            flow=flow,
        )
        return

    # Xato: holatni saqlab qolamiz (parolni qayta kiritish mumkin).
    await message.answer(
        result.message or texts.error_generic(), reply_markup=cancel_reply_keyboard(user)
    )


# ---------------------------------------------------------------------------
# /login
# ---------------------------------------------------------------------------
@router.message(or_f(Command("login"), F.text.in_((BTN_LOGIN, text_of(Emoji.KEY, BTN_LOGIN)))))
async def cmd_login(
    message: Message,
    user: User,
    storage: StorageService,
    share: ShareService,
    tg_storage: TelegramStorage,
    settings: Settings,
    state: FSMContext,
    # ``command`` ixtiyoriy — "🔑 Login" reply tugmasi orqali kelganda bo'lmaydi.
    command: CommandObject | None = None,
) -> None:
    """``/login <login> <parol>`` — hisobni tasdiqlash."""
    service = _service(storage.session, storage, settings)
    login, password = _split_credentials(command.args if command else None)

    if not login:
        await state.set_state(AuthState.waiting_login)
        await state.update_data(flow="login")
        await message.answer(texts.login_prompt(), reply_markup=cancel_reply_keyboard(user))
        return

    if not password:
        await state.set_state(AuthState.waiting_password)
        await state.update_data(flow="login", login=login)
        await message.answer(
            f"{Emoji.KEY.plain()} <b>{login}</b> parolini kiriting:",
            reply_markup=cancel_reply_keyboard(user),
        )
        return

    result = await service.login_telegram(_caller_id(message, user), login, password)
    if result.ok and result.user is not None:
        await _finish_auth_success(
            message,
            result.user,
            storage,
            share,
            tg_storage,
            settings,
            state,
            login=login,
            flow="login",
        )
        return

    await message.answer(
        result.message or texts.error_generic(), reply_markup=auth_reply_keyboard(user)
    )


# ---------------------------------------------------------------------------
# /profiles — ko'p profilli sessiyalar
# ---------------------------------------------------------------------------
@router.message(Command("profiles"))
async def cmd_profiles(message: Message, user: User, storage: StorageService) -> None:
    """Profillar ro'yxati: qaysi biri faol va boshqasiga qanday o'tiladi.

    Parollar hech qachon ko'rsatilmaydi; o'tish faqat ``/login`` orqali
    (login + parol), shuning uchun ro'yxatni ko'rish xavfsiz.
    """
    from html import escape

    from services.profiles import ProfileService

    caller = _caller_id(message, user)
    profiles = await ProfileService(storage.session).list_profiles(caller)
    lines = ["👤 <b>Profillaringiz</b>", ""]
    if not profiles:
        lines.append("Hali profil yo'q.")
    for profile in profiles:
        active = profile.telegram_id == user.telegram_id and user.is_signed_in
        mark = "🟢" if active else "⚪"
        suffix = " — <i>faol</i>" if active else ""
        lines.append(f"{mark} <code>{escape(profile.login or '—')}</code>{suffix}")
    lines += [
        "",
        "↪️ Boshqa profilga o'tish: <code>/login login parol</code>",
        "➕ Yangi profil ochish: <code>/register login parol</code>",
        "",
        "<i>Profilga muvaffaqiyatli kirilganda oldingi sessiya yakunlanadi. "
        "Fayllar har bir profilda alohida saqlanadi.</i>",
    ]
    await message.answer("\n".join(lines))


# ---------------------------------------------------------------------------
# /logout, /password, /id
# ---------------------------------------------------------------------------
@router.message(Command("logout"))
async def cmd_logout(
    message: Message,
    user: User,
    storage: StorageService,
    settings: Settings,
    state: FSMContext,
) -> None:
    """``/logout`` — sessiyani tugatish (parol saqlanib qoladi)."""
    service = _service(storage.session, storage, settings)
    await state.clear()
    await service.logout(user.telegram_id)
    await message.answer(texts.logout_ok(), reply_markup=auth_reply_keyboard(user))


@router.message(Command("password"))
async def cmd_password(
    message: Message,
    command: CommandObject | None,
    user: User,
    storage: StorageService,
    settings: Settings,
    state: FSMContext,
) -> None:
    """``/password <eski> <yangi>`` — parolni yangilash."""
    service = _service(storage.session, storage, settings)
    old, new = _split_credentials(command.args if command else None)

    if not user.has_password:
        await message.answer(texts.no_account_yet(), reply_markup=auth_reply_keyboard(user))
        return

    if not old or not new:
        await message.answer(
            texts.password_change_prompt(), reply_markup=cancel_reply_keyboard(user)
        )
        return

    result = await service.change_password(user.telegram_id, old, new)
    await message.answer(
        result.message
        or (texts.password_changed() if result.ok else texts.password_wrong_current()),
        reply_markup=main_reply_keyboard(user, web_app_url=settings.web.web_app_url)
        if result.ok
        else cancel_reply_keyboard(user),
    )


@router.message(Command("id"))
async def cmd_id(message: Message, user: User) -> None:
    """``/id`` — Telegram ID ni ko'rsatish (admin qo'shish uchun kerak)."""
    await message.answer(
        f"{Emoji.INFO.plain()} Telegram ID: <code>{user.telegram_id}</code>\n"
        f"Hisob: <code>{user.login or '—'}</code>"
    )
