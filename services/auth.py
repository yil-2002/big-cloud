"""Autentifikatsiya xizmati — ``/register``, ``/login``, ``/logout`` mantiqi.

Bu modul **business logic** qatlami: login/parol tekshiruvi, urinishlar
hisoblagichi, vaqtinchalik bloklash va audit yozuvlari shu yerda. aiogram
handlerlari (``bot/handlers/auth.py``) faqat matn va klaviatura bilan ishlaydi,
shuning uchun mantiq bot va REST API o'rtasida bir xil bo'ladi.

Xavfsizlik qoidalari
--------------------
1. Parollar :mod:`db.models.password` (PBKDF2-HMAC-SHA256 + salt) bilan
   hashlanadi — bazada hech qachon ochiq parol saqlanmaydi.
2. Taqqoslash ``hmac.compare_digest`` orqali (timing hujumiga chidamli).
3. ``AUTH_MAX_ATTEMPTS`` martadan keyin hisob ``AUTH_LOCKOUT`` soniyaga
   qulflanadi; qulflash audit jurnaliga tushadi.
4. Bloklangan foydalanuvchi (``is_blocked``) parolni to'g'ri kiritsa ham
   o'tmaydi — admin bloki har doim ustun.
5. Login nomi unikal: ``users.login`` ustida UNIQUE indeks mavjud, shuningdek
   :meth:`StorageService.is_login_taken` orqali oldindan tekshiriladi.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings, get_settings
from db.models import AuditAction, User
from services.audit import AuditService
from services.profiles import ProfileService
from services.storage import StorageService

logger = logging.getLogger(__name__)

__all__ = ["AuthOutcome", "AuthResult", "AuthService", "validate_login"]

#: Login nomi uchun ruxsat etilgan belgilar: harf, raqam, ``_`` va ``.``.
_LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9_.]{3,32}$")
#: Foydalanuvchiga ko'rsatiladigan qoida matni (uz/en bir xil).
LOGIN_RULES = "3-32 belgi: harf, raqam, _ va ."


class AuthOutcome(StrEnum):
    """:meth:`AuthService.login` natijasi variantlari."""

    OK = "ok"
    UNKNOWN_LOGIN = "unknown_login"
    WRONG_PASSWORD = "wrong_password"
    LOCKED = "locked"
    BLOCKED = "blocked"
    NO_ACCOUNT = "no_account"


@dataclass(slots=True)
class AuthResult:
    """Handler'ga qaytariladigan javob obyekti."""

    outcome: AuthOutcome
    user: User | None = None
    #: Qolgan urinishlar soni (``wrong_password`` holatida muhim).
    attempts_left: int = 0
    #: Qulf ochilishigacha qolgan soniya.
    lock_seconds: int = 0
    message: str | None = None

    @property
    def ok(self) -> bool:
        """Muvaffaqiyatli autentifikatsiya."""
        return self.outcome is AuthOutcome.OK


def validate_login(login: str) -> str | None:
    """Login nomini tekshirish; xato matnini yoki ``None`` qaytaradi."""
    cleaned = (login or "").strip()
    if not cleaned:
        return "Login bo'sh bo'lishi mumkin emas."
    if not _LOGIN_PATTERN.fullmatch(cleaned):
        return f"Login noto'g'ri formatda. Qoida: {LOGIN_RULES}"
    if cleaned.lower() in {"admin", "root", "bot", "system", "null", "none"}:
        return "Bu login nomi band (tizim uchun saqlangan)."
    return None


class AuthService:
    """Hisob yaratish / tizimga kirish / chiqish."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        storage: StorageService | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.storage = storage or StorageService(session, settings=self.settings)
        self.audit = AuditService(session)

    # ------------------------------------------------------------------
    # Ro'yxatdan o'tish
    # ------------------------------------------------------------------
    async def register(
        self,
        telegram_id: int,
        login: str,
        password: str,
        *,
        source: str = "bot",
        ip_address: str | None = None,
    ) -> AuthResult:
        """``/register <login> <password>`` — yangi hisob ochish.

        Login unikal. Telegram akkauntida hisob allaqachon bo'lsa, **qo'shimcha
        profil** ochiladi (ko'p profilli rejim) va foydalanuvchi darhol unga
        o'tadi; oldingi profil sessiyasi yakunlanadi. Alohida ``/login`` kerak emas.
        """
        auth = self.settings.auth
        if not auth.allow_registration:
            await self.audit.safe_log(
                AuditAction.REGISTER,
                actor_id=telegram_id,
                detail="registration disabled",
                success=False,
                source=source,
            )
            return AuthResult(
                AuthOutcome.NO_ACCOUNT,
                message="Ro'yxatdan o'tish hozircha yopilgan.",
            )

        login_error = validate_login(login)
        if login_error:
            return AuthResult(AuthOutcome.NO_ACCOUNT, message=login_error)

        password_error = self.validate_password(password)
        if password_error:
            return AuthResult(AuthOutcome.NO_ACCOUNT, message=password_error)

        cleaned_login = login.strip()
        if await self.storage.is_login_taken(cleaned_login):
            await self.audit.safe_log(
                AuditAction.REGISTER,
                actor_id=telegram_id,
                detail=f"login taken: {cleaned_login}",
                success=False,
                source=source,
                ip_address=ip_address,
            )
            return AuthResult(
                AuthOutcome.NO_ACCOUNT,
                message=f"«{cleaned_login}» logini allaqachon band. Boshqasini tanlang.",
            )

        user = await self.storage.get_user_or_raise(telegram_id)
        if user.is_blocked:
            return AuthResult(AuthOutcome.BLOCKED, user=user, message="Hisobingiz bloklangan.")
        profiles = ProfileService(self.session)
        if user.has_password and user.login:
            # Bu Telegram akkauntida hisob bor — yangi, alohida profil ochamiz.
            user = await profiles.create_extra_profile(telegram_id, cleaned_login, password)
        else:
            user.register_credentials(cleaned_login, password)
            user.is_blocked = False
            await profiles.activate(telegram_id, user)
        self.session.add(user)
        await self.audit.log(
            AuditAction.REGISTER,
            actor_id=telegram_id,
            detail=f"account created: {cleaned_login}",
            extra={"login": cleaned_login},
            success=True,
            source=source,
            ip_address=ip_address,
        )
        await self.session.commit()
        await self.storage.cache.invalidate_user(telegram_id)
        await self.storage.cache.invalidate_user(user.telegram_id)
        logger.info("Registered account %s for %d", cleaned_login, telegram_id)
        return AuthResult(AuthOutcome.OK, user=user, message=f"Hisob yaratildi: {cleaned_login}")

    # ------------------------------------------------------------------
    # Tizimga kirish
    # ------------------------------------------------------------------
    async def login(
        self,
        login: str,
        password: str,
        *,
        telegram_id: int | None = None,
        source: str = "bot",
        ip_address: str | None = None,
    ) -> AuthResult:
        """``/login <login> <parol>`` — hisobni tasdiqlash.

        ``telegram_id`` berilsa hisob shu Telegram akkauntiga bog'lanadi
        (``is_authenticated=True``), ya'ni bot ichidagi kirish. API'da esa
        faqat tekshiruv bajariladi (JWT berish ``api/routes/auth.py`` da).
        """
        auth = self.settings.auth
        cleaned_login = (login or "").strip()
        user = await self.storage.get_user_by_login(cleaned_login)

        if user is None:
            await self.audit.safe_log(
                AuditAction.LOGIN_FAIL,
                actor_id=telegram_id,
                detail=f"unknown login: {cleaned_login}",
                success=False,
                source=source,
                ip_address=ip_address,
            )
            return AuthResult(
                AuthOutcome.UNKNOWN_LOGIN,
                message="Bunday login topilmadi. /register bilan hisob oching.",
            )

        if user.is_blocked:
            await self.audit.safe_log(
                AuditAction.LOGIN_FAIL,
                actor_id=user.telegram_id,
                detail="blocked account attempted login",
                success=False,
                source=source,
                ip_address=ip_address,
            )
            return AuthResult(
                AuthOutcome.BLOCKED,
                user=user,
                message="Hisobingiz administrator tomonidan bloklangan.",
            )

        if user.is_locked:
            return AuthResult(
                AuthOutcome.LOCKED,
                user=user,
                lock_seconds=user.lock_seconds_left,
                message=(
                    f"Hisob vaqtincha qulflangan. "
                    f"{_human_seconds(user.lock_seconds_left)} dan keyin urinib ko'ring."
                ),
            )

        if not user.check_password(password or ""):
            attempts = user.register_failed_login(
                lock_seconds=(
                    auth.lockout_seconds
                    if user.failed_login_attempts + 1 >= auth.max_attempts
                    else 0
                )
            )
            locked_now = user.is_locked
            self.session.add(user)
            await self.audit.log(
                AuditAction.ACCOUNT_LOCKED if locked_now else AuditAction.LOGIN_FAIL,
                actor_id=user.telegram_id,
                target_id=user.telegram_id,
                detail=(
                    f"account locked for {auth.lockout_seconds}s"
                    if locked_now
                    else f"wrong password (attempt {attempts}/{auth.max_attempts})"
                ),
                extra={"attempts": attempts, "login": user.login},
                success=False,
                source=source,
                ip_address=ip_address,
            )
            await self.session.commit()
            await self.storage.cache.invalidate_user(user.telegram_id)
            return AuthResult(
                AuthOutcome.LOCKED if locked_now else AuthOutcome.WRONG_PASSWORD,
                user=user,
                attempts_left=max(0, auth.max_attempts - attempts),
                lock_seconds=user.lock_seconds_left,
                message=(
                    f"Hisob {auth.lockout_seconds // 60} daqiqaga qulflandi."
                    if locked_now
                    else f"Parol noto'g'ri. Qolgan urinishlar: {max(0, auth.max_attempts - attempts)}"
                ),
            )

        # Profilga o'tish (ko'p profilli sessiyalar): fayllar KO'CHIRILMAYDI.
        # Chaqiruvchining oldingi profil sessiyasi va profilning boshqa
        # Telegram'dagi eski sessiyasi yakunlanadi (``ProfileService.activate``).
        caller_id = telegram_id if telegram_id is not None else user.telegram_id
        if telegram_id is not None:
            # Ustunni to'g'ridan-to'g'ri o'qiymiz (expire bo'lgan qatorda ham xavfsiz).
            caller_blocked = await self.session.scalar(
                select(User.is_blocked).where(User.telegram_id == telegram_id)
            )
            # Admin bloki har doim ustun: bloklangan Telegram akkaunt boshqa
            # profilga kirib blokni chetlab o'ta olmaydi.
            if caller_blocked:
                await self.audit.safe_log(
                    AuditAction.LOGIN_FAIL,
                    actor_id=telegram_id,
                    target_id=user.telegram_id,
                    detail="login from a blocked telegram id rejected",
                    success=False,
                    source=source,
                    ip_address=ip_address,
                )
                return AuthResult(
                    AuthOutcome.BLOCKED,
                    user=user,
                    message="Hisobingiz administrator tomonidan bloklangan.",
                )
        previous_holder = user.session_holder

        # Muvaffaqiyat: profil chaqiruvchiga o'tadi (hisoblagichlar tozalanadi).
        await ProfileService(self.session).activate(caller_id, user)
        if user.password_is_outdated():
            user.set_password(password)  # eski sxemadan PBKDF2 ga o'tkazish
            await self.audit.safe_log(
                AuditAction.PASSWORD_CHANGE,
                actor_id=user.telegram_id,
                detail="hash upgraded to current PBKDF2 parameters",
                source=source,
            )
        self.session.add(user)

        await self.audit.log(
            AuditAction.LOGIN_OK,
            actor_id=caller_id,                 # kim kirdi (Telegram)
            target_id=user.telegram_id,         # qaysi profilga
            detail=f"login ok: {user.login}",
            extra={"login": user.login},
            success=True,
            source=source,
            ip_address=ip_address,
        )
        await self.session.commit()
        for stale in {user.telegram_id, caller_id, previous_holder}:
            if stale is not None:
                await self.storage.cache.invalidate_user(stale)
        logger.info("Telegram %s signed in to profile %s", caller_id, user.login)
        return AuthResult(AuthOutcome.OK, user=user, message=f"Xush kelibsiz, {user.login}!")

    async def login_telegram(
        self,
        telegram_id: int,
        login: str,
        password: str,
        *,
        source: str = "bot",
    ) -> AuthResult:
        """Bot ichidagi ``/login``: shu Telegram akkaunt profilga o'tadi.

        ``telegram_id`` — chaqiruvchining HAQIQIY Telegram ID si (profil ID emas).
        Login+parol to'g'ri bo'lsa profil aktiv bo'ladi; fayllar ko'chirilmaydi,
        eski sessiyalar yakunlanadi — :meth:`ProfileService.activate`.
        """
        user = await self.storage.get_user_any(telegram_id)
        if user is not None and user.has_password and not (login or "").strip():
            # "/login" bo'sh keldi — mavjud hisobni qayta faollashtirish.
            return AuthResult(
                AuthOutcome.NO_ACCOUNT,
                user=user,
                message=f"Format: /login {user.login or '<login>'} <parol>",
            )
        return await self.login(
            login, password, telegram_id=telegram_id, source=source
        )

    # ------------------------------------------------------------------
    # Chiqish / parol
    # ------------------------------------------------------------------
    async def logout(self, telegram_id: int, *, source: str = "bot") -> bool:
        """``/logout`` — sessiyani tugatish (parol saqlanib qoladi)."""
        user = await self.storage.get_user_any(telegram_id)
        if user is None:
            return False
        user.logout()
        self.session.add(user)
        await self.audit.safe_log(
            AuditAction.LOGOUT, actor_id=telegram_id, detail="user logged out", source=source
        )
        await self.session.commit()
        await self.storage.cache.invalidate_user(telegram_id)
        return True

    async def change_password(
        self,
        telegram_id: int,
        old_password: str,
        new_password: str,
        *,
        source: str = "bot",
    ) -> AuthResult:
        """``/password`` — joriy parolni tasdiqlab yangisini o'rnatish."""
        user = await self.storage.get_user_any(telegram_id)
        if user is None or not user.has_password:
            return AuthResult(AuthOutcome.NO_ACCOUNT, message="Avval /register qiling.")
        if not user.check_password(old_password or ""):
            await self.audit.safe_log(
                AuditAction.PASSWORD_CHANGE,
                actor_id=telegram_id,
                detail="wrong current password",
                success=False,
                source=source,
            )
            return AuthResult(AuthOutcome.WRONG_PASSWORD, user=user, message="Joriy parol noto'g'ri.")
        error = self.validate_password(new_password)
        if error:
            return AuthResult(AuthOutcome.NO_ACCOUNT, user=user, message=error)
        user.set_password(new_password)
        user.reset_failed_logins()
        self.session.add(user)
        await self.audit.safe_log(
            AuditAction.PASSWORD_CHANGE,
            actor_id=telegram_id,
            detail="password changed by user",
            source=source,
        )
        await self.session.commit()
        await self.storage.cache.invalidate_user(telegram_id)
        return AuthResult(AuthOutcome.OK, user=user, message="Parol yangilandi.")

    def validate_password(self, password: str) -> str | None:
        """Parol uzunligi/qoidasi tekshiruvi; xato matnini qaytaradi."""
        auth = self.settings.auth
        value = password or ""
        if len(value) < auth.min_password_length:
            return (
                f"Parol juda qisqa: kamida {auth.min_password_length} belgi bo'lsin."
            )
        if len(value) > auth.max_password_length:
            return f"Parol juda uzun: ko'pi bilan {auth.max_password_length} belgi."
        if value.strip() != value:
            return "Parol boshida/oxirida bo'shliq bo'lmasin."
        return None

    def touch(self, user: User) -> None:
        """``last_seen_at`` ni yangilash (auth middleware har update'da chaqiradi)."""
        from datetime import datetime

        user.last_seen_at = datetime.now(UTC)
        self.session.add(user)

    async def ensure_account(self, user: User, password: str | None = None) -> dict[str, Any]:
        """Admin uchun qisqa hisob hisoboti (``/diyor`` -> 👥)."""
        return {
            "telegram_id": user.telegram_id,
            "login": user.login,
            "name": user.display_name,
            "authenticated": bool(user.is_authenticated),
            "blocked": bool(user.is_blocked),
            "locked": user.is_locked,
            "failed_attempts": int(user.failed_login_attempts or 0),
            "last_login_at": user.last_login_at,
            "last_seen_at": user.last_seen_at,
            "has_password": user.has_password,
            "generated_password": password,
        }


def _human_seconds(seconds: int) -> str:
    """``125`` -> ``2 daqiqa`` (admin/handler matnlari uchun)."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} soniya"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} daqiqa"
    return f"{minutes // 60} soat"
