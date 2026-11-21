"""Private Archive Vault — har bir yuklangan faylning barqabor nusxasi.

Nima uchun kerak?
-----------------
``file_id`` — bu Telegram serverlaridagi obyektga havola. Foydalanuvchi o'z
xabarini o'chirsa, havola ko'pincha ishlaydi, ammo bu **kafolatlanmagan**.
Shuning uchun har bir yuklama maxfiy arxiv kanaliga (``ARCHIVE_CHANNEL_ID``)
nusxalanadi va kanal ``message_id`` si bazaga yoziladi: fayl endi bot nazorati
ostidagi kanalda ham yashaydi.

Nima uchun ``copy_message`` (``forward_message`` emas)?
------------------------------------------------------
Forward qilingan xabar asl nusxaga bog'liq bo'lib qoladi va "Forwarded from"
yozuvini ko'rsatadi. ``copy_message`` esa kanalga **mustaqil** obyekt yaratadi —
asl xabar o'chsa ham nusxa yashaydi.

Kanal ``file_id`` si haqida
---------------------------
Bot API'da kanaldagi xabarni to'g'ridan-to'g'ri o'qish usuli yo'q
(``getMessages`` mavjud emas), shuning uchun kanal ``file_id`` si **talab
qilinganda** (``recover_file``) aniqlanadi: arxiv nusxasi foydalanuvchi chatiga
qayta nusxalanadi, yangi ``file_id`` o'qiladi va bazaga yoziladi. Bu yondashuv
yuklama jarayonini sekinlashtirmaydi va 2 GB fayllar uchun ham tarmoq trafigi
talab qilmaydi (Telegram faylni server ichida ko'chiradi).

Xavfsizlik va ishonchlilik
--------------------------
* kanal ID si faqat ``.env`` dan o'qiladi, bot kanalda **admin** bo'lishi shart;
* ``ARCHIVE_ENABLED=false`` yoki kanal ko'rsatilmagan bo'lsa qatlam o'chadi va
  yuklama odatdagidek davom etadi (arxiv hech qachon asosiy ishni buzmaydi);
* muvaffaqiyatsiz urinishlar ``ARCHIVE_MAX_ATTEMPTS`` marta qaytariladi
  (chiziqli backoff) va ``audit_logs`` ga ``archive_failed`` sifatida tushadi.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
)
from aiogram.types import Message
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from config import Settings, get_settings
from db.database import get_session_factory
from db.models import AuditAction, File
from services.audit import AuditService

logger = logging.getLogger(__name__)

__all__ = [
    "ArchiveResult",
    "ArchiveStatus",
    "ArchiveVault",
    "ensure_archived_for_stream",
    "get_vault",
    "reset_vault",
    "set_vault",
]

#: Fon vazifalarini GC yutib qo'ymasligi uchun kuchli havolalar saqlanadi.
_background_tasks: set[asyncio.Task[Any]] = set()


def _utcnow() -> datetime:
    """Timezone-aware ``now()`` (SQLite/PostgreSQL uchun bir xil)."""
    return datetime.now(UTC)


@dataclass(slots=True)
class ArchiveResult:
    """Arxivlash natijasi (muvaffaqiyatsiz bo'lsa ``ok=False``)."""

    ok: bool
    chat_id: str | None = None
    message_id: int | None = None
    file_id: str | None = None
    error: str | None = None
    attempts: int = 0

    def __bool__(self) -> bool:
        """``if result:`` — qulaylik uchun."""
        return self.ok


@dataclass(slots=True)
class ArchiveStatus:
    """``/diyor`` -> ⚙️ Tizim sozlamalari uchun kanal holati."""

    enabled: bool
    configured: bool
    channel_id: str
    chat_exists: bool = False
    chat_title: str | None = None
    chat_type: str | None = None
    bot_is_admin: bool = False
    bot_can_post: bool = False
    archived_files: int = 0
    pending_files: int = 0
    error: str | None = None

    @property
    def healthy(self) -> bool:
        """Arxiv ishlayotganini bildiruvchi yagona belgi."""
        return self.enabled and self.configured and self.chat_exists and self.bot_can_post

    def as_line(self) -> str:
        """Admin panel uchun bir qatorli holat matni."""
        if not self.configured:
            return "❌ ARCHIVE_CHANNEL_ID sozlanmagan"
        if not self.enabled:
            return "⏸️ Arxiv o'chirilgan (ARCHIVE_ENABLED=false)"
        if not self.chat_exists:
            return f"❌ Kanal topilmadi: {self.error or self.channel_id}"
        if not self.bot_is_admin:
            return "⚠️ Bot kanalda admin emas"
        if not self.bot_can_post:
            return "⚠️ Botda xabar yuborish huquqi yo'q"
        return (
            f"✅ {self.chat_title or self.channel_id} — arxiv faol "
            f"({self.archived_files} nusxalangan, {self.pending_files} kutilmoqda)"
        )

    def to_dict(self) -> dict[str, Any]:
        """REST API uchun JSON ko'rinishi."""
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "channel_id": self.channel_id,
            "chat_exists": self.chat_exists,
            "chat_title": self.chat_title,
            "chat_type": self.chat_type,
            "bot_is_admin": self.bot_is_admin,
            "bot_can_post": self.bot_can_post,
            "archived_files": self.archived_files,
            "pending_files": self.pending_files,
            "healthy": self.healthy,
            "error": self.error,
        }


def extract_file_id(message: Message) -> str | None:
    """Xabardagi media obyektining ``file_id`` sini ajratib olish."""
    if message.document is not None:
        return message.document.file_id
    if message.video is not None:
        return message.video.file_id
    if message.photo:
        # Eng katta o'lchamli foto — asl nusxa bilan bir xil sifat.
        return max(message.photo, key=lambda photo: photo.file_size or 0).file_id
    if message.audio is not None:
        return message.audio.file_id
    if message.voice is not None:
        return message.voice.file_id
    if message.animation is not None:
        return message.animation.file_id
    if message.video_note is not None:
        return message.video_note.file_id
    if message.sticker is not None:
        return message.sticker.file_id
    return None


class ArchiveVault:
    """Arxiv kanali bilan ishlash mantiqi."""

    def __init__(
        self,
        bot: Bot,
        *,
        settings: Settings | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.bot = bot
        self.settings = settings or get_settings()
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # Holat
    # ------------------------------------------------------------------
    @property
    def channel_id(self) -> str:
        """Sozlangan arxiv kanali (bo'sh bo'lsa qatlam o'chirilgan)."""
        return self.settings.archive.channel_id

    @property
    def enabled(self) -> bool:
        """``True`` bo'lsa har bir yuklama arxivlanadi."""
        return self.settings.archive.configured

    def session_maker(self) -> async_sessionmaker[AsyncSession]:
        """Sessiya fabrikasi — fon vazifalari o'z sessiyasini ochadi."""
        return self._session_factory or get_session_factory()

    async def status(self, *, with_counters: bool = True) -> ArchiveStatus:
        """Kanal holatini tekshirish (admin huquqlari + arxiv statistikasi)."""
        archive = self.settings.archive
        state = ArchiveStatus(
            enabled=archive.enabled,
            configured=bool(archive.channel_id),
            channel_id=archive.channel_id or "<unset>",
        )
        if not archive.channel_id:
            return state

        try:
            chat = await self.bot.get_chat(archive.channel_id)
        except (TelegramBadRequest, TelegramForbiddenError, TelegramAPIError) as exc:
            state.error = str(exc).splitlines()[0][:200]
            logger.warning("Archive channel %s unreachable: %s", archive.channel_id, exc)
            return state

        state.chat_exists = True
        state.chat_title = chat.title or chat.username or str(chat.id)
        state.chat_type = chat.type

        try:
            me = await self.bot.me()
            member = await self.bot.get_chat_member(chat.id, me.id)
            state.bot_is_admin = member.status in {"administrator", "creator"}
            can_post = getattr(member, "can_post_messages", None)
            # Kanal (channel) uchun aniq huquq; guruh uchun umumiy yuborish huquqi.
            state.bot_can_post = bool(can_post) if can_post is not None else state.bot_is_admin
        except (TelegramBadRequest, TelegramForbiddenError, TelegramAPIError) as exc:
            state.error = str(exc).splitlines()[0][:200]

        if with_counters:
            state.archived_files, state.pending_files = await self.counters()
        return state

    async def counters(self) -> tuple[int, int]:
        """``(arxivlangan, arxivlanmagan)`` fayllar soni."""
        from sqlalchemy import func, select

        factory = self.session_maker()
        async with factory() as session:
            archived = int(
                await session.scalar(
                    select(func.count())
                    .select_from(File)
                    .where(File.archive_message_id.is_not(None))
                )
                or 0
            )
            total = int(await session.scalar(select(func.count()).select_from(File)) or 0)
        return archived, max(0, total - archived)

    # ------------------------------------------------------------------
    # Arxivlash
    # ------------------------------------------------------------------
    async def archive_message(
        self,
        message: Message,
        *,
        file_uuid: Any,
        telegram_id: int,
        file_name: str,
        caption: str | None = None,
    ) -> ArchiveResult:
        """Xabarni arxiv kanaliga nusxalash va natijani bazaga yozish.

        Yuklama tasdiqlangandan **keyin** chaqiriladi: arxiv muvaffaqiyatsiz
        bo'lsa ham foydalanuvchi fayli saqlangan bo'ladi (faqat audit xatosi
        yoziladi).
        """
        archive = self.settings.archive
        if not archive.configured:
            return ArchiveResult(ok=False, error="archive disabled")

        text = self._vault_caption(file_uuid)
        attempts = max(1, archive.max_attempts)
        last_error: str | None = None

        for attempt in range(1, attempts + 1):
            try:
                copied = await self.bot.copy_message(
                    chat_id=archive.channel_id,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                    caption=text,
                    disable_notification=True,
                )
                result = ArchiveResult(
                    ok=True,
                    chat_id=str(archive.channel_id),
                    message_id=int(copied.message_id),
                    # Kanal file_id si keyinroq (recover_file) aniqlanadi.
                    file_id=None,
                    attempts=attempt,
                )
                await self._persist(file_uuid, result)
                await self._audit(
                    AuditAction.FILE_ARCHIVED,
                    telegram_id,
                    detail=f"{file_name} -> {archive.channel_id}#{result.message_id}",
                    extra={"file_uuid": str(file_uuid), "attempts": attempt},
                    success=True,
                )
                logger.info(
                    "Archived %s as %s#%s", file_name, archive.channel_id, result.message_id
                )
                return result
            except TelegramForbiddenError as exc:
                # Bot kanaldan chiqarilgan — qayta urinish foydasiz.
                last_error = f"forbidden: {exc}"
                logger.error("Archive channel rejected the bot: %s", exc)
                break
            except TelegramBadRequest as exc:
                last_error = f"bad request: {exc}"
                logger.warning("Archive copy failed (attempt %d): %s", attempt, exc)
                if "not found" in str(exc).lower():
                    break  # asl xabar yo'q — qayta urinish ma'nosiz
            except TelegramAPIError as exc:
                last_error = str(exc).splitlines()[0][:200]
                logger.warning("Archive attempt %d/%d failed: %s", attempt, attempts, exc)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("Unexpected archive error")

            if attempt < attempts:
                await asyncio.sleep(archive.retry_delay * attempt)

        await self._audit(
            AuditAction.ARCHIVE_FAILED,
            telegram_id,
            detail=f"{file_name}: {last_error}",
            extra={"file_uuid": str(file_uuid), "attempts": attempts},
            success=False,
        )
        return ArchiveResult(ok=False, error=last_error, attempts=attempts)

    async def recover_file(
        self,
        record: File,
        *,
        to_chat_id: int,
        delete_probe: bool = True,
    ) -> str | None:
        """Arxiv nusxasidan kanal ``file_id`` sini olish va bazaga yozish.

        Qachon ishlatiladi: asl ``file_id`` Telegram tomonidan rad etilganda
        ("wrong file identifier") yoki faylni mustaqil nusxadan chaqirish kerak
        bo'lganda.

        Mexanika (tarmoq trafigisiz):
        1. ``copy_message(channel -> to_chat_id)`` — javobda **to'liq Message**
           qaytadi, chunki bot o'zi yuborgan xabarni ko'radi;
        2. shu xabardan media ``file_id`` o'qiladi — bu kanal obyekti uchun
           amalqli havola;
        3. vaqtinchalik xabar o'chiriladi (``delete_probe=True``);
        4. ``files.archive_file_id`` yangilanadi, keyingi chaqiruvlar tekin.
        """
        if not self.enabled or not record.archive_message_id:
            return None

        try:
            probe = await self.bot.copy_message(
                chat_id=to_chat_id,
                from_chat_id=self.channel_id,
                message_id=record.archive_message_id,
                disable_notification=True,
            )
        except TelegramAPIError as exc:
            logger.warning("Recovery copy failed for %s: %s", record.id, exc)
            return None

        # copy_message javobi MessageId; kanal file_id sini olish uchun xabarni
        # bot ko'rgan holda qayta yuboramiz va javob Message dan o'qiymiz.
        channel_file_id: str | None = None
        try:
            forwarded = await self.bot.forward_message(
                chat_id=to_chat_id,
                from_chat_id=self.channel_id,
                message_id=record.archive_message_id,
                disable_notification=True,
            )
            channel_file_id = extract_file_id(forwarded)
            if delete_probe:
                await self.bot.delete_message(to_chat_id, forwarded.message_id)
        except TelegramAPIError as exc:
            logger.debug("Could not read the archive copy for %s: %s", record.id, exc)
        finally:
            if delete_probe and probe is not None:
                try:
                    await self.bot.delete_message(to_chat_id, probe.message_id)
                except TelegramAPIError:  # pragma: no cover
                    logger.debug("Could not delete the recovery probe message")

        if channel_file_id:
            record.archive_file_id = channel_file_id
            factory = self.session_maker()
            async with factory() as session:
                await session.execute(
                    update(File)
                    .where(File.id == record.id)
                    .values(archive_file_id=channel_file_id)
                    .execution_options(synchronize_session=False)
                )
                await session.commit()
            logger.info("Recovered file_id for %s from the archive", record.id)
        return channel_file_id

    def _vault_caption(self, file_uuid: Any) -> str:
        """Arxiv nusxasiga yoziladigan izoh — **anonim**.

        Maxfiylik: ilgari izohda ``user=<telegram id>`` va ``name=<fayl nomi>``
        ochiq yozilardi — kanalga kirgan har kim kimning qanday fayli borligini
        ko'rardi. Endi faqat prefiks va fayl UUID'idan olingan qisqa belgi
        (bog'lanish bazada: ``files.archive_message_id``). Audit izi bazadagi
        ``audit_logs`` da qoladi (baza nusxasi shifrlangan).
        """
        prefix = self.settings.archive.caption_prefix or "☁️ ARCHIVE"
        ref = hashlib.sha256(str(file_uuid).encode()).hexdigest()[:12]
        return f"{prefix} · {ref}"

    # ------------------------------------------------------------------
    # Ichki yordamchilar
    # ------------------------------------------------------------------
    async def _persist(self, file_uuid: Any, result: ArchiveResult) -> None:
        """Arxiv ma'lumotini ``files`` qatoriga yozish (o'z sessiyasi bilan)."""
        if not result.ok:
            return
        factory = self.session_maker()
        async with factory() as session:
            await session.execute(
                update(File)
                .where(File.id == file_uuid)
                .values(
                    archive_chat_id=result.chat_id,
                    archive_message_id=result.message_id,
                    archive_file_id=result.file_id,
                    archived_at=_utcnow(),
                )
                .execution_options(synchronize_session=False)
            )
            await session.commit()

    async def _audit(
        self,
        action: str,
        telegram_id: int,
        *,
        detail: str | None,
        extra: dict[str, Any] | None = None,
        success: bool = True,
    ) -> None:
        """Arxiv hodisasini audit jurnaliga yozish (xatoga chidamli)."""
        try:
            factory = self.session_maker()
            async with factory() as session:
                await AuditService(session).log(
                    action,
                    actor_id=telegram_id,
                    detail=detail,
                    extra=extra,
                    success=success,
                    source="vault",
                    commit=True,
                )
        except Exception:
            logger.debug("Could not write audit entry %s", action, exc_info=True)

    # ------------------------------------------------------------------
    # Fon rejimi
    # ------------------------------------------------------------------
    def schedule(
        self,
        coro_factory: Callable[[], Coroutine[Any, Any, ArchiveResult]],
    ) -> asyncio.Task[ArchiveResult] | None:
        """Vazifani fon rejimida ishga tushirish (``ARCHIVE_BLOCKING=false``).

        Foydalanuvchi tasdiqni darhol ko'radi; arxivlash parallel ketadi.
        """
        if not self.enabled:
            return None
        try:
            task: asyncio.Task[ArchiveResult] = asyncio.create_task(
                coro_factory(), name="archive-vault"
            )
        except RuntimeError:  # pragma: no cover - ishlayotgan loop yo'q
            logger.warning("No running event loop; archive task skipped")
            return None
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        task.add_done_callback(_log_task_error)
        return task


def _log_task_error(task: asyncio.Task[Any]) -> None:
    """Fon vazifasi xatosini jurnalga yozish (jimгина yo'qolmasin)."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Archive background task failed: %s", exc)


# ---------------------------------------------------------------------------
# Singleton (bot bir marta yaratiladi)
# ---------------------------------------------------------------------------
_vault: ArchiveVault | None = None


def set_vault(vault: ArchiveVault | None) -> None:
    """Global nusxani o'rnatish (``bot/setup.py`` startup'da chaqiradi)."""
    global _vault
    _vault = vault


def get_vault(bot: Bot | None = None, *, settings: Settings | None = None) -> ArchiveVault | None:
    """Global arxiv xizmati.

    ``bot`` berilsa va hali yaratilmagan bo'lsa — yaratadi. Bot bo'lmasa va
    singleton o'rnatilmagan bo'lsa ``None`` qaytadi (chaqiruvchi arxivsiz
    davom etadi).
    """
    global _vault
    if _vault is None and bot is not None:
        _vault = ArchiveVault(bot, settings=settings)
    return _vault


def reset_vault() -> None:
    """Testlar uchun singletonni tozalash."""
    global _vault
    _vault = None

async def ensure_archived_for_stream(record: Any, *, settings: Settings, storage: Any, bot: Bot | None) -> bool:
    """Arxiv nusxasi yo'q bo'lsa, uni shu zahoti yaratadi (idempotent)."""
    if record.archive_message_id is not None:
        return True
    if not settings.archive.configured or bot is None:
        return False

    try:
        chat_id = settings.archive.numeric_id or settings.archive.channel_id
        caption = f"{settings.archive.caption_prefix}\nuser={record.user_id}\nname={record.file_name}"[:1024]

        message = await bot.send_document(
            chat_id=chat_id,
            document=record.best_file_id,
            caption=caption,
            disable_notification=True,
        )

        record.mark_archived(chat_id=chat_id, message_id=message.message_id)
        if message.document:
            record.archive_file_id = message.document.file_id

        await storage.session.commit()
        return True
    except Exception as exc:
        logger.warning("ensure_archived_for_stream failed for %s: %s", record.id, exc)
        return False
