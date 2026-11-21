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
import uuid
from dataclasses import dataclass
from datetime import UTC
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings, get_settings
from db.models import AuditAction, File, Folder, User
from services.audit import AuditService
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

        Mavjud hisobni qayta ro'yxatdan o'tkazish mumkin emas: login unikal.
        Muvaffaqiyatli holatda foydalanuvchi darhol ``is_authenticated=True``
        bo'ladi, ya'ni alohida ``/login`` talab qilinmaydi.
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
        if user.has_password and user.login:
            # Hisob allaqachon mavjud — qayta yaratish o'rniga login taklif qilamiz.
            return AuthResult(
                AuthOutcome.NO_ACCOUNT,
                user=user,
                message=(
                    f"Sizda allaqachon hisob bor: «{user.login}». "
                    "Shu hisob bilan kiring yoki parolingizni yangilang."
                ),
            )

        user.register_credentials(cleaned_login, password)
        user.is_blocked = False
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

        # Boshqa Telegram akkauntidan kirish: hisobni joriy akkauntga to'liq
        # o'tkazamiz (login/parol to'g'ri — parol kalit hisoblanadi).
        if telegram_id is not None and telegram_id != user.telegram_id:
            # Ustunni to'g'ridan-to'g'ri o'qiymiz: oldingi bulk-update qator
            # atributlarini "expire" qilgan bo'lsa ham MissingGreenlet bo'lmasin.
            blocked_target = await self.session.scalar(
                select(User.is_blocked).where(User.telegram_id == telegram_id)
            )
            # Admin bloki har doim ustun: bloklangan yangi akkauntga hisobni
            # o'tkazmaymiz (aks holda blok chetlab o'tilardi).
            if blocked_target:
                await self.audit.safe_log(
                    AuditAction.LOGIN_FAIL,
                    actor_id=telegram_id,
                    target_id=user.telegram_id,
                    detail="transfer to a blocked telegram id rejected",
                    success=False,
                    source=source,
                    ip_address=ip_address,
                )
                return AuthResult(
                    AuthOutcome.BLOCKED,
                    user=user,
                    message="Hisobingiz administrator tomonidan bloklangan.",
                )
            user = await self._transfer_account(
                user, telegram_id, source=source, ip_address=ip_address
            )

        # Muvaffaqiyat: hisoblagichni tozalash + (kerak bo'lsa) hashni yangilash.
        user.mark_authenticated()
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
            actor_id=user.telegram_id,
            detail=f"login ok: {user.login}",
            extra={"login": user.login},
            success=True,
            source=source,
            ip_address=ip_address,
        )
        await self.session.commit()
        await self.storage.cache.invalidate_user(user.telegram_id)
        logger.info("User %d authenticated as %s", user.telegram_id, user.login)
        return AuthResult(AuthOutcome.OK, user=user, message=f"Xush kelibsiz, {user.login}!")

    async def login_telegram(
        self,
        telegram_id: int,
        login: str,
        password: str,
        *,
        source: str = "bot",
    ) -> AuthResult:
        """Bot ichidagi ``/login``: hisob joriy Telegram ID ga o'tkaziladi.

        Login+parol to'g'ri bo'lsa, hisob (fayllar, papkalar, sozlamalar bilan)
        shu akkauntga ko'chadi — batafsil :meth:`_transfer_account`.
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

    # ------------------------------------------------------------------
    # Hisobni boshqa Telegram akkauntga o'tkazish
    # ------------------------------------------------------------------
    async def _transfer_account(
        self,
        user: User,
        target_id: int,
        *,
        source: str,
        ip_address: str | None = None,
    ) -> User:
        """Hisobni to'liq yangi Telegram ID ga o'tkazadi.

        Login+parol to'g'ri bo'lganda chaqiriladi: login, parol hash, holat,
        kvota, sozlamalar, fayllar va papkalar yangi akkauntga ko'chadi; eski
        ``User`` qatori (egalik to'liq o'tgach) o'chiriladi. **Fayl va papka
        qatorlari o'chirilmaydi** — unique constraint ziddiyatlari nom/
        fingerprint'ni o'zgartirish bilan hal qilinadi. Butun amal bitta
        tranzaksiyada bajariladi va bir marta commit qilinadi. Bu yo'lda bot
        orqali hech qanday fayl/xabar yuborilmaydi — faqat DB egaligi, audit
        yozuvi va kesh tozalash.
        """
        old_id = user.telegram_id
        # Oldingi bulk-update qatorni "expire" qilgan bo'lishi mumkin — barcha
        # maydonlarni o'qishdan oldin aniq qayta yuklaymiz (MissingGreenlet yo'q).
        await self.session.refresh(user)
        # Eski qatorni o'chirishdan oldin ko'chiriladigan maydonlarni eslab qolamiz.
        login = user.login
        password_hash = user.password_hash
        snapshot: dict[str, Any] = {
            "is_authorized": bool(user.is_authorized),
            "is_authenticated": bool(user.is_authenticated),
            "failed_login_attempts": int(user.failed_login_attempts or 0),
            "locked_until": user.locked_until,
            "last_login_at": user.last_login_at,
            "last_seen_at": user.last_seen_at,
            "admin_note": user.admin_note,
            "storage_quota": user.storage_quota,
            "settings": dict(user.settings or {}),
            "language_code": user.language_code,
        }
        old_used = int(user.storage_used or 0)

        target = await self.session.get(User, target_id)
        if target is None:
            # Maqsad qator yo'q — yaratamiz (Telegram profil maydonlari bo'sh
            # qoladi; ular keyingi update'da ``_sync_profile`` bilan to'ladi).
            target = User(telegram_id=target_id)
            self.session.add(target)
            await self.session.flush()
        else:
            # Avvalgi operatsiyalar (masalan, kvota bulk-update) atributni
            # expire qilgan bo'lishi mumkin — aniq qayta yuklaymiz.
            await self.session.refresh(target)
        target_used = int(target.storage_used or 0)

        # ``login`` ustidagi UNIQUE: avval eski qatordan bo'shatib flush qilamiz,
        # shundan keyingina yangi qatorga yozamiz — aks holda constraint buziladi.
        user.login = None
        user.password_hash = None
        self.session.add(user)
        await self.session.flush()

        # 1) Papkalar (ota-onadan boshlab) — daraxt buzilmasligi uchun avval.
        #    Qatorlar SONI saqlanadi: dublikatlar o'chirilmaydi, faqat nomi
        #    ziddiyatsiz variantga o'zgartiriladi.
        folder_remap = await self._move_folders(old_id, target_id)
        # 2) Fayllar — egalik ko'chadi, hech bir qator o'chirilmaydi.
        await self._move_files(old_id, target_id, folder_remap)

        # 3) Hisob maydonlarini ko'chirish (yangi qatorning Telegram profili qoladi).
        target.login = login
        target.password_hash = password_hash
        for field, value in snapshot.items():
            setattr(target, field, value)
        # Eski hisobning hajmi + maqsadning o'z hajmi (hech bir fayl o'chmaydi).
        target.storage_used = old_used + target_used
        self.session.add(target)
        await self.session.flush()

        # 4) Eski qatorni o'chirish. Bu FAQAT barcha fayl/papkalar yangi id ga
        #    o'tgandan keyin bajariladi — aks holda ``ondelete=CASCADE`` ularni
        #    o'chirib yuborardi. Endi eski qatorning hech narsasi qolmagan.
        await self.session.delete(user)
        await self.session.flush()

        await self.audit.log(
            AuditAction.LOGIN_OK,
            actor_id=target_id,
            target_id=old_id,
            detail=f"account transferred: {login} from {old_id} to {target_id}",
            success=True,
            source=source,
            ip_address=ip_address,
        )
        await self.session.commit()
        await self.storage.cache.invalidate_user(old_id)
        await self.storage.cache.invalidate_user(target_id)
        logger.info("Account %s transferred from %d to %d", login, old_id, target_id)
        return target

    async def _move_folders(
        self, old_id: int, target_id: int
    ) -> dict[uuid.UUID, uuid.UUID]:
        """Eski egadan papkalarni ko'chiradi va ``parent_id`` daraxtini saqlaydi.

        Qaytaradi: eski papka id -> yangi papka id xaritasi. **Qatorlar soni
        o'zgarmaydi**: ``(user_id, parent_id, name)`` ziddiyati bo'lsa papka
        o'chirilmaydi, balki "Nom (2)" ko'rinishidagi ziddiyatsiz nomga
        o'zgartiriladi — papka va uning butun ichidagilari saqlanib qoladi.
        """
        folders = list(
            await self.session.scalars(select(Folder).where(Folder.user_id == old_id))
        )
        if not folders:
            return {}

        existing = list(
            await self.session.scalars(select(Folder).where(Folder.user_id == target_id))
        )
        used: set[tuple[uuid.UUID | None, str]] = {
            (folder.parent_id, _folder_key(folder.name)) for folder in existing
        }

        remap: dict[uuid.UUID, uuid.UUID] = {}
        for folder in _folders_parents_first(folders):
            parent_id: uuid.UUID | None = None
            if folder.parent_id is not None:
                # Ota-ona avval ko'chgan (topologik tartib); noma'lum bo'lsa ildiz.
                parent_id = remap.get(folder.parent_id)
            key = (parent_id, _folder_key(folder.name))
            if key in used:
                folder.name = _unique_folder_name(folder.name, parent_id, used)
                key = (parent_id, _folder_key(folder.name))
            folder.parent_id = parent_id
            folder.user_id = target_id
            used.add(key)
            remap[folder.id] = folder.id
            self.session.add(folder)
        await self.session.flush()
        return remap

    async def _move_files(
        self,
        old_id: int,
        target_id: int,
        folder_remap: dict[uuid.UUID, uuid.UUID],
    ) -> None:
        """Fayllarni yangi egaga o'tkazadi — **hech bir qator o'chirilmaydi**.

        ``(user_id, file_unique_id)`` ziddiyati bo'lsa qator saqlanib qoladi:
        faqat ``file_unique_id`` (Telegram kontent-fingerprint'i) ziddiyatsiz
        variantga o'zgartiriladi. Faylning o'zi — ``file_id``, hajmi va boshqa
        maydonlari — o'zgarmaydi va ishlashda davom etadi.
        """
        files = list(
            await self.session.scalars(select(File).where(File.user_id == old_id))
        )
        if not files:
            return

        used: set[str] = set(
            await self.session.scalars(
                select(File.file_unique_id).where(File.user_id == target_id)
            )
        )
        for record in files:
            if record.file_unique_id in used:
                record.file_unique_id = _unique_fingerprint(record.file_unique_id, used)
            used.add(record.file_unique_id)
            record.user_id = target_id
            if record.folder_id is not None:
                record.folder_id = folder_remap.get(record.folder_id, record.folder_id)
            self.session.add(record)
        await self.session.flush()

    # ------------------------------------------------------------------
    # Yordamchilar
    # ------------------------------------------------------------------
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


def _folder_key(name: str | None) -> str:
    """Papka nomini ziddiyat solishtirish uchun normallashtirish."""
    return (name or "").strip().casefold()


def _unique_folder_name(
    name: str | None,
    parent_id: uuid.UUID | None,
    used: set[tuple[uuid.UUID | None, str]],
) -> str:
    """``(user_id, parent_id, name)`` uchun ziddiyatsiz papka nomi yasaydi.

    Papka qatori o'chirilmaydi — nom "Nom (2)", "Nom (3)" ko'rinishida
    o'zgartiriladi (255 belgi chegarasidan oshmaydi).
    """
    base = (name or "").strip() or "Papka"
    counter = 2
    while True:
        suffix = f" ({counter})"
        candidate = f"{base[: 255 - len(suffix)]}{suffix}"
        if (parent_id, _folder_key(candidate)) not in used:
            return candidate
        counter += 1


def _unique_fingerprint(original: str, used: set[str]) -> str:
    """``(user_id, file_unique_id)`` uchun ziddiyatsiz fingerprint yasaydi.

    Fayl qatori o'chirilmaydi; faqat Telegram kontent-fingerprint'i (64 belgi
    chegarasida) "~2", "~3" qo'shimchasi bilan noyob qilinadi. ``file_id``,
    hajm va boshqa maydonlar tegilmaydi.
    """
    base = (original or "file")[:64]
    counter = 2
    while True:
        suffix = f"~{counter}"
        candidate = f"{base[: 64 - len(suffix)]}{suffix}"
        if candidate not in used:
            return candidate
        counter += 1


def _folders_parents_first(folders: list[Folder]) -> list[Folder]:
    """Papkalarni ota-ona boladan oldin keladigan tartibda qaytaradi.

    ``parent_id`` zanjiri bo'yicha yuradigan DFS; sikl yoki yetim qatorlar
    (nazariy jihatdan bo'lmasligi kerak) ro'yxat oxiriga qo'shiladi — shunda
    hech bir papka e'tibordan chetda qolmaydi.
    """
    by_id = {folder.id: folder for folder in folders}
    children: dict[uuid.UUID | None, list[Folder]] = {}
    for folder in folders:
        parent = folder.parent_id if folder.parent_id in by_id else None
        children.setdefault(parent, []).append(folder)

    ordered: list[Folder] = []
    seen: set[uuid.UUID] = set()
    stack = list(children.get(None, []))
    while stack:
        folder = stack.pop()
        if folder.id in seen:
            continue
        seen.add(folder.id)
        ordered.append(folder)
        stack.extend(children.get(folder.id, []))
    ordered.extend(folder for folder in folders if folder.id not in seen)
    return ordered

