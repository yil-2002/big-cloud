"""📤 Yuklama quvuri: foydalanuvchi yuborgan har qanday media — bulut obyekti.

Oqim::

    media bilan xabar
      → StorableMedia / TooLargeMedia filtrlari   (media_info injeksiya qilinadi)
      → AlbumMiddleware                            (albom buferlanadi)
      → AuthGuardMiddleware                        (hisob tasdiqlanganmi?)
      → StorageService.ensure_fits                 (hajm cheklovi; kvota cheksiz)
      → StorageService.add_file                    (dedupe + hisoblagich + kesh)
      → ArchiveVault.archive_message               (maxfiy kanalga nusxa)
      → tasdiq kartasi (fayl amallari bilan)

Ikki muhim ``hugecloud`` xususiyati shu yerda ishlaydi:

1. **Dedupe** — ``file_unique_id`` allaqachon bazada bo'lsa, fayl qayta
   yozilmaydi: mavjud qator darhol qaytariladi (``StoredFile.is_new is False``)
   va foydalanuvchi "allaqachon bor" xabarini ko'radi;
2. **Arxiv** — har bir yangi fayl ``ARCHIVE_CHANNEL_ID`` ga nusxalanadi
   (``ARCHIVE_BLOCKING=false`` bo'lsa fon rejimida, tasdiqni kechiktirmaydi).

Xatolar aniq va foydali matnga aylanadi (hajm / kvota / dublikat / qo'llab-
quvvatlanmaydigan tur) — hech qachon xom traceback ko'rsatilmaydi.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.types import Message

from bot import texts
from bot.filters.media import StorableMedia, TooLargeMedia
from bot.keyboards.inline import file_actions_keyboard
from bot.utils.emojis import Emoji
from bot.utils.formatters import escape_html, format_size
from config import Settings
from db.models import File, User
from services.exceptions import (
    DuplicateFileError,
    InsufficientQuotaError,
    TooLargeError,
)
from services.storage import StorageService, StoredFile
from services.telegram import (
    TelegramFileInfo,
    extract_file_info,
    is_supported_media,
)
from services.vault import ArchiveResult, ArchiveVault, get_vault

logger = logging.getLogger(__name__)

router = Router(name="upload")

#: Bulut diskiga saqlanib bo'lmaydigan xabar turlari.
_NON_STORABLE = (
    F.location
    | F.venue
    | F.poll
    | F.dice
    | F.invoice
    | F.successful_payment
    | F.new_chat_members
    | F.left_chat_member
    | F.chat_photo
)


@router.message(TooLargeMedia())
async def on_too_large(message: Message, media_info: TelegramFileInfo, size_limit: int) -> None:
    """``STORAGE_MAX_FILE_SIZE`` dan oshib ketgan yuklamani aniq raqamlar bilan rad etish."""
    await message.answer(texts.too_large(media_info.file_size, size_limit))


@router.message(StorableMedia())
async def on_media(
    message: Message,
    media_info: TelegramFileInfo,
    user: User,
    storage: StorageService,
    settings: Settings,
    album: list[Message] | None = None,
) -> None:
    """Bitta faylni (yoki albom a'zosini) foydalanuvchi bulutiga saqlash."""
    quota = storage.quota_for(user)
    in_album = bool(album and len(album) > 1)

    # 1) Oldindan tekshirish: bitta fayl limiti va kvota (kvota 0 = cheksiz).
    try:
        storage.ensure_fits(user, media_info.file_size)
    except TooLargeError:
        await message.answer(
            texts.too_large(media_info.file_size, storage.settings.storage.max_file_size)
        )
        return
    except InsufficientQuotaError as exc:
        await message.answer(texts.quota_exceeded(**_quota_details(exc, user, quota)))
        return

    # 2) Bazaga yozish. ``file_unique_id`` bo'yicha dedupe shu yerda ishlaydi:
    #    dublikat bo'lsa StoredFile(is_new=False) qaytadi (xato emas!).
    try:
        stored = await storage.add_file(
            telegram_id=user.telegram_id, **media_info.to_kwargs()
        )
    except DuplicateFileError:
        # ``raise_on_duplicate`` yuborilmagan, shuning uchun bu holat kamdan-kam
        # (IntegrityError poygasi) — baribir foydali matn ko'rsatamiz.
        if not in_album:
            await message.answer(texts.dedupe_hit(media_info.file_name))
        return
    except InsufficientQuotaError as exc:
        await message.answer(texts.quota_exceeded(**_quota_details(exc, user, quota)))
        return

    record = stored.record

    # 3) Arxiv kanali: yangi fayllarni nusxalash (dedupe hit'da nusxa allaqachon bor).
    archive_note = await _archive(
        message,
        record=record,
        stored=stored,
        user=user,
        settings=settings,
        in_album=in_album,
    )

    # 4) "Bo'sh joy" qatori shu yuklamani aks ettirishi uchun yangilaymiz.
    await storage.session.refresh(user)

    if not user.setting("notify_uploads", True):
        await message.answer(texts.file_saved(record, user=user, quota=quota))
        return

    if in_album:
        # Albom a'zolari uchun ixcham tasdiq: klaviatura spam qilmasin.
        await message.answer(
            f"{Emoji.CHECK.plain()} <b>{escape_html(record.file_name)}</b> · "
            f"{format_size(record.file_size)}\n"
            f"{texts.storage_line(user.storage_used, quota, storage.quota_label(user))}"
        )
        return

    if not stored.is_new:
        # Dedupe: fayl allaqachon bulutda bor — darhol ochib berdik.
        await message.answer(
            texts.dedupe_hit(record.file_name),
            reply_markup=file_actions_keyboard(record, user=user, folder=record.folder),
        )
        return

    confirmation = texts.file_saved(record, user=user, quota=quota)
    if archive_note:
        confirmation = f"{confirmation}\n\n{archive_note}"
    await message.answer(
        confirmation,
        reply_markup=file_actions_keyboard(record, user=user, folder=record.folder),
    )


async def _archive(
    message: Message,
    *,
    record: File,
    stored: StoredFile,
    user: User,
    settings: Settings,
    in_album: bool,
) -> str | None:
    """Arxiv kanaliga nusxalash (bloklaydigan yoki fon rejimida).

    Qaytarilgan matn tasdiq xabariga qo'shiladi; fon rejimida ``None``.
    Arxiv xatosi yuklamani **hech qachon** bekor qilmaydi.
    """
    if not stored.is_new or record.is_archived:
        return None  # allaqachon nusxalangan

    vault = get_vault(settings=settings)
    if vault is None or not vault.enabled:
        return None

    async def _do_archive() -> ArchiveResult:
        res = await vault.archive_message(
            message,
            file_uuid=record.id,
            telegram_id=user.telegram_id,
            file_name=record.file_name,
            caption=record.caption,
        )
        if res.ok:
            try:
                await message.delete()
            except Exception as exc:
                logger.warning("service message cleanup failed for %s: %s", user.telegram_id, exc)
        return res

    if not settings.archive.blocking:
        # Fon rejimi: foydalanuvchi tasdiqni darhol ko'radi.
        vault.schedule(_do_archive)
        return None

    result: ArchiveResult = await _do_archive()
    if in_album:
        return None
    return texts.archived_ok(record.file_name) if result.ok else texts.archived_fail()


@router.message(_NON_STORABLE)
async def on_non_storable(message: Message) -> None:
    """Kontakt/lokatsiya/so'rovnoma saqlanmasligini tushuntirish."""
    await message.answer(texts.unsupported_media())


@router.message(F.contact)
async def on_contact(message: Message, user: User, storage: StorageService) -> None:
    if message.contact and message.contact.user_id == message.from_user.id:
        user.phone = message.contact.phone_number
        await storage.session.commit()
        await message.answer("✅ Raqam saqlandi")
    else:
        await message.answer("faqat o'z raqamingizni ulashing")


@router.message(F.text & ~F.text.startswith("/"))
async def on_plain_text(message: Message, user: User, settings: Settings) -> None:
    """Fayl o'rniga matn yozgan foydalanuvchini yo'naltirish.

    Reply-klaviatura tugmalari :mod:`bot.handlers.commands` da marshrutlanadi,
    shuning uchun bu yerga faqat erkin matn keladi: yuklama maslahati + dashboard.
    """
    from bot.keyboards.reply import main_reply_keyboard

    await message.answer(
        texts.upload_hint(),
        reply_markup=main_reply_keyboard(user, web_app_url=settings.web.web_app_url),
    )


# ---------------------------------------------------------------------------
# Yordamchilar
# ---------------------------------------------------------------------------
def _quota_details(exc: InsufficientQuotaError, user: User, quota: int) -> dict[str, Any]:
    """Domain xatosidan :func:`bot.texts.quota_exceeded` uchun kwargs yig'ish."""
    details = exc.details or {}
    return {
        "required": int(details.get("required", 0)),
        "free": int(details.get("free", user.free_space(quota))),
        "quota": int(details.get("quota", quota)),
        "used": int(details.get("used", user.storage_used)),
    }


async def store_from_message(
    message: Message,
    *,
    user: User,
    storage: StorageService,
    settings: Settings | None = None,
    archive: bool = True,
) -> File | None:
    """Qayta ishlatiladigan yordamchi (deep link, admin, testlar): bitta xabarni saqlash.

    Yaratilgan qatorni qaytaradi; agar xabarda saqlanadigan media bo'lmasa
    ``None``. ``archive=True`` bo'lsa arxiv kanali ham ishlatiladi (fon rejimida).
    """
    if not is_supported_media(message):
        return None
    info = extract_file_info(message)
    if info is None:
        return None
    storage.ensure_fits(user, info.file_size)
    stored = await storage.add_file(telegram_id=user.telegram_id, **info.to_kwargs())

    if archive and stored.is_new:
        vault: ArchiveVault | None = get_vault(settings=settings)
        if vault is not None and vault.enabled:
            async def _do_archive_store():
                res = await vault.archive_message(
                    message,
                    file_uuid=stored.record.id,
                    telegram_id=user.telegram_id,
                    file_name=stored.record.file_name,
                    caption=stored.record.caption,
                )
                if res.ok:
                    try:
                        await message.delete()
                    except Exception as exc:
                        logger.warning("service message cleanup failed for %s: %s", user.telegram_id, exc)
            vault.schedule(_do_archive_store)
    return stored.record
