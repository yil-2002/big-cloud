"""🔐 Autentifikatsiya darvozasi — **barcha** sessiyalar shu yerdan o'tadi.

Talab (product spec): "yangi va mavjud barcha sessiyalarni ushlab, bot
imkoniyatlarini berishdan oldin ro'yxatdan o'tish / tizimga kirishni majburiy
qilish; deep-link parametrlari uzluksiz ishlashi kerak".

Bu middleware va :mod:`bot.handlers.start` bitta siyosat manbaidan —
:mod:`bot.session` — foydalanadi, shuning uchun qoidalar ikki joyda
takrorlanmaydi va bir-biriga zid kelmaydi.

Qoidalar (tartib muhim):

1. foydalanuvchisiz hodisa (kanal posti) → tekshirilmaydi;
2. ``users.is_blocked`` → bloklash ekrani (boshqa hech narsa ishlamaydi);
3. ``ADMIN_ID`` → har doim o'tadi;
4. ``AUTH_REQUIRED=false`` → ochiq bot (legacy xatti-harakat);
5. ``users.is_authenticated`` → to'liq ruxsat;
6. faol FSM dialogi (``AuthState`` / ``AdminState`` / ``PasswordState`` /
   ``ImportState``) → o'tkaziladi, aks holda dialog o'zi bloklanib qoladi;
7. ruxsat etilgan buyruq (``/start``, ``/login``, ``/register``, ``/cancel``,
   ``/help``, ``/id``, ``/logout``) → o'tadi;
8. **qolgani** → darvoza ekrani va update yutiladi.

Deep-link uzluksizligi
----------------------
``/start <payload>`` tasdiqlanmagan foydalanuvchidan kelsa, payload FSM'ga
park qilinadi (:func:`bot.session.park_payload`) va ekranda "havolangiz
saqlanib turibdi" deyiladi. Login/register muvaffaqiyatli tugagach
:mod:`bot.handlers.auth` uni avtomatik ijro etadi — foydalanuvchi havolani
qayta bosishi shart emas.

Tejamkorlik
-----------
* ``last_seen_at`` minutiga bir marta yangilanadi (``/diyor`` → "🟢 Faol
  sessiyalar" shu ustundan oziqlanadi);
* "yangi yoki qaytib kelgan" farqi **DB so'rovisiz** aniqlanadi
  (``last_login_at`` / ``storage_used`` / ``last_seen_at``) — har update'da
  ``files`` jadvalini sanash sekin bo'lardi;
* rad javoblari Telegram xatosini yutadi: darvoza hech qachon bot'ni yiqitmaydi.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, ClassVar, cast

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineQuery, Message, ReplyKeyboardMarkup, TelegramObject

from bot import texts
from bot.keyboards.reply import auth_reply_keyboard, cancel_reply_keyboard
from bot.session import (
    SessionDecision,
    SessionKind,
    SessionResolution,
    has_parked_payload,
    park_payload,
    resolve_session,
)
from bot.states import AdminState, AuthState, ImportState, PasswordState
from config import Settings, get_settings
from db.models import User

logger = logging.getLogger(__name__)

__all__ = [
    "ALLOWED_COMMANDS",
    "ALLOWED_STATE_GROUPS",
    "AuthGuardMiddleware",
    "auth_override",
    "resolve_session_for",
    "set_auth_override",
]

#: Ruxsat etilgan buyruqlar (``bot.session.GUEST_COMMANDS`` bilan bir xil).
#:
#: ``/diyor`` ham shu yerda: boshqaruv paneli parol bilan himoyalangan va
#: ``ADMIN_ID`` bo'sh bo'lganda yagona kirish yo'li shu. Darvoza uni bloklasa,
#: foydalanuvchi hisobiga kira olmasa (masalan parolni unutgan bo'lsa) panel
#: umuman ochilmasdi — handler parol so'rashga ham yetib bormasdi.
ALLOWED_COMMANDS: frozenset[str] = frozenset(
    {
        "/start",
        "/login",
        "/register",
        "/logout",
        "/profiles",
        "/cancel",
        "/help",
        "/id",
        "/diyor",
    }
)

#: FSM holatlari — dialog davom etayotganda darvoza xalaqit bermasligi kerak.
#: :class:`ImportState` qo'shilgan: Mini App "Import" tugmasi boshlaydigan oqim.
ALLOWED_STATE_GROUPS: tuple[type, ...] = (AuthState, AdminState, PasswordState, ImportState)

#: Ish vaqtida ``/diyor`` o'rnatadigan ustun (``None`` = ``.env`` qiymati).
_override: bool | None = None


def set_auth_override(value: bool | None) -> None:
    """Autentifikatsiya talabini ish vaqtida yoqish/o'chirish."""
    global _override
    _override = value
    logger.info("Auth requirement overridden at runtime: %s", value)


def auth_override() -> bool | None:
    """Joriy ustun qiymati (testlar va admin panel uchun)."""
    return _override


def _command_of(text: str | None) -> str | None:
    """Matndagi buyruqni ajratish: ``/login a b`` -> ``/login``."""
    if not text:
        return None
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    # ``/login@Huge_cloud_bot`` ko'rinishini ham qabul qilamiz.
    token = stripped.split(maxsplit=1)[0]
    return token.split("@", 1)[0].lower()


def _start_payload_of(text: str | None) -> str:
    """``/start <payload>`` dan payload'ni ajratish (deep-link parklash uchun)."""
    if not text or _command_of(text) != "/start":
        return ""
    parts = text.strip().split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def _plain(html_text: str) -> str:
    """HTML teglarini olib tashlash (callback alert matni 200 belgiga sig'ishi kerak)."""
    return re.sub(r"<[^>]+>", "", html_text).replace("&lt;", "<").replace("&gt;", ">")


def resolve_session_for(user: User | None, *, gate_enabled: bool, is_admin: bool) -> SessionResolution:
    """DB so'rovisiz sessiya tahlili (``bot.session.resolve_session`` o'rami).

    "Yangi" va "qaytib kelgan mehmon" farqi hisob metama'lumotlaridan olinadi:

    * ``last_login_at`` — hech qachon kirmagan bo'lsa, bu yangi sessiya;
    * ``storage_used`` — fayl yuklagan bo'lsa, albatta qaytib kelgan;
    * ``last_seen_at`` — middleware'ning o'zi yozadi, ya'ni **birinchi** update'da
      hali ``None`` (yangi), keyingilarida to'ldirilgan (qaytib kelgan).
    """
    has_activity = True
    if user is not None:
        has_activity = bool(
            user.last_login_at is not None
            or (user.storage_used or 0) > 0
            or user.last_seen_at is not None
        )
    return resolve_session(
        user,
        gate_enabled=gate_enabled,
        is_admin=is_admin,
        has_any_activity=has_activity,
    )


class AuthGuardMiddleware(BaseMiddleware):
    """Hisob tasdiqlanmaguncha bulut funksiyalarini bloklab turadi."""

    #: So'nggi ``last_seen_at`` yozuvi (har update'da DB ga urmaslik uchun).
    _last_touch: ClassVar[dict[int, datetime]] = {}
    #: Qancha vaqtda bir marta "seen" yangilanadi.
    TOUCH_INTERVAL_SECONDS: ClassVar[int] = 60

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        """Darvoza faolmi (runtime ustun ``.env`` dan ustun)."""
        if _override is not None:
            return _override
        return self.settings.auth.required

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = cast(User | None, data.get("user"))
        if user is None:
            # Foydalanuvchisiz hodisa (kanal posti va h.k.) — tekshirmaymiz.
            return await handler(event, data)

        # Tartib muhim: avval sessiyani aniqlaymiz, keyin ``last_seen_at`` ni
        # yangilaymiz. Aks holda ``_touch`` xotiradagi qatorga vaqt yozib
        # qo'yadi va **birinchi** update ham "qaytib kelgan mehmon" deb
        # hisoblanadi (yangi foydalanuvchi salomlashuvini hech qachon ko'rmaydi).
        resolution = resolve_session_for(
            user,
            gate_enabled=self.enabled,
            is_admin=bool(data.get("is_admin")) or self.settings.bot.is_admin(user.telegram_id),
        )
        await self._touch(user, data)

        # 1-5) Admin / o'chirilgan darvoza / tasdiqlangan hisob → o'tadi.
        if resolution.decision is SessionDecision.ALLOW:
            return await handler(event, data)

        # 6) Bloklangan hisob: faqat bloklash ekrani.
        if resolution.decision is SessionDecision.BLOCKED:
            await self._reject(
                event,
                texts.blocked_message(resolution.block_reason),
                reply_keyboard=cancel_reply_keyboard(user),
            )
            return None

        # 7) Faol FSM dialogi → xalaqit bermaymiz (aks holda ``/register``
        #    dialogi o'zi darvozaga tiqilib qoladi).
        state = cast(FSMContext | None, data.get("state"))
        if state is not None:
            current = await state.get_state()
            if current and any(
                str(current).startswith(group.__name__) for group in ALLOWED_STATE_GROUPS
            ):
                return await handler(event, data)

        # 8) Ruxsat etilgan buyruqlar. ``/start <payload>`` bo'lsa payload'ni
        #    park qilamiz — login'dan keyin avtomatik davom etadi.
        if isinstance(event, Message):
            command = _command_of(event.text)
            if command in ALLOWED_COMMANDS:
                if command == "/start":
                    payload = _start_payload_of(event.text)
                    if payload:
                        await park_payload(state, payload)
                return await handler(event, data)

        # 9) Qolgan hammasi — darvoza ekrani (update yutiladi).
        await self._show_gate(event, user, resolution, state)
        return None

    # ------------------------------------------------------------------
    async def _show_gate(
        self,
        event: TelegramObject,
        user: User,
        resolution: SessionResolution,
        state: FSMContext | None,
    ) -> None:
        """Yangi / qaytib kelgan sessiya uchun darvoza ekranini ko'rsatish.

        Klaviatura: ``Login`` / ``Ro'yxatdan o'tish`` / ``Yordam`` (reply).
        Callback query holatida esa faqat alert ko'rsatiladi — chunki Telegram
        callback orqali reply klaviaturani o'zgartira olmaydi.
        """
        parked = await has_parked_payload(state)
        if resolution.kind is SessionKind.NEW:
            message = texts.gate_new_session(
                self.settings.bot.username,
                self.settings.web.web_app_url or None,
                parked,
            )
        else:
            message = texts.gate_returning_session(user.login, parked=parked)

        await self._reject(event, message, reply_keyboard=auth_reply_keyboard(user))

    async def _touch(self, user: User, data: dict[str, Any]) -> None:
        """``last_seen_at`` ni yangilash (tejamkor: minutiga bir marta)."""
        now = datetime.now(UTC)
        # Xotira cheksiz o'smasligi uchun juda katta lug'atni tozalaymiz.
        if len(AuthGuardMiddleware._last_touch) > 10_000:
            AuthGuardMiddleware._last_touch.clear()
        previous = AuthGuardMiddleware._last_touch.get(user.telegram_id)
        if previous is not None and (now - previous).total_seconds() < self.TOUCH_INTERVAL_SECONDS:
            return
        AuthGuardMiddleware._last_touch[user.telegram_id] = now

        session = data.get("session")
        if session is None:
            return
        try:
            user.last_seen_at = now
            session.add(user)
            await session.commit()
        except Exception:
            logger.debug("Could not touch last_seen_at for %s", user.telegram_id, exc_info=True)
            try:
                await session.rollback()
            except Exception:  # rollback ham muvaffaqiyatsiz bo'lsa — statistika yo'li, ish to'xtamaydi
                logger.debug("last_seen_at rollback failed", exc_info=True)

    async def _reject(
        self,
        event: TelegramObject,
        text: str,
        *,
        reply_keyboard: ReplyKeyboardMarkup | InlineKeyboardMarkup | None = None,
    ) -> None:
        """Rad javobini yuborish: xabarga — matn+klaviatura, callback'ga — alert.

        Telegram xatolari **yutiladi**: darvoza hech qachon update'ni yiqitmaydi
        (aks holda webhook'da xato log'i shovqin qiladi, foydalanuvchi esa
        javobsiz qoladi).
        """
        if isinstance(event, CallbackQuery):
            try:
                await event.answer(text=_plain(text)[:200], show_alert=True, cache_time=5)
            except Exception:
                logger.debug("callback.answer failed in auth guard", exc_info=True)
            return

        if isinstance(event, InlineQuery):
            try:
                # Inline natija bo'sh qoldiriladi: chat'ga xabar yozib bo'lmaydi.
                await event.answer(results=[], cache_time=1, is_personal=True)
            except Exception:
                logger.debug("inline answer failed in auth guard", exc_info=True)
            return

        if isinstance(event, Message):
            markup: ReplyKeyboardMarkup | InlineKeyboardMarkup = (
                reply_keyboard or auth_reply_keyboard()
            )
            try:
                await event.answer(text, reply_markup=markup)
            except Exception:
                logger.debug("message.answer failed in auth guard", exc_info=True)


def _is_first_contact(event: TelegramObject) -> bool:
    """``/start`` yoki birinchi xabar -> to'liq tushuntirish matni."""
    if isinstance(event, Message):
        command = _command_of(event.text)
        return command in {"/start", None}
    return False
