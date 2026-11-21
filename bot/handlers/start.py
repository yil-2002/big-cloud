"""``/start`` and command handlers, including the deep-link payload router.

Deep-link grammar (``t.me/<bot>?start=<payload>``):

===========================  =====================================================
payload                      behaviour
===========================  =====================================================
*(empty)*                    register + dashboard
``file_<uuid.hex>``          open one of **my** files (bot-generated link)
``folder_<uuid.hex>``        open one of **my** folders
``<share token>``            public share: send the file and save a copy
``ref_<telegram_id>``        referral entry (stored in settings for analytics)
===========================  =====================================================

Every branch re-validates ownership/visibility in the service layer — a payload
is never trusted as an authorization decision.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from aiogram import Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.keyboards.hugecloud import mini_app_home_kb
from bot.keyboards.inline import main_menu_keyboard
from bot.keyboards.reply import auth_reply_keyboard, main_reply_keyboard
from bot.utils.emojis import Emoji
from config import Settings
from db.models import File, User
from services.exceptions import (
    DuplicateFileError,
    FileNotFoundError,
    FolderNotFoundError,
    InsufficientQuotaError,
    InvalidPassCodeError,
    LinkExpiredError,
    LinkNotFoundError,
    LinkRevokedError,
)
from services.share import ShareService
from services.storage import StorageService
from services.telegram import TelegramStorage

logger = logging.getLogger(__name__)

router = Router(name="start")

#: Deep-link payload prefixes we understand.
_PREFIX_FILE = "file_"
_PREFIX_FOLDER = "folder_"
_PREFIX_REF = "ref_"


@router.message(CommandStart())
async def on_start_command(
    message: Message,
    command: CommandObject | None,
    user: User,
    storage: StorageService,
    share: ShareService,
    tg_storage: TelegramStorage,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
) -> None:
    """``/start`` va ``/start <payload>`` — yagona kirish nuqtasi.

    Oqim (product spec: "barcha sessiyalarni ushlab, avval ro'yxatdan
    o'tish/kirishni majburiy qilish; deep-link uzluksiz bo'lsin"):

    1. payload ajratiladi (``t.me/<bot>?start=<payload>`` → ``/start <payload>``);
    2. hisob tasdiqlanmagan bo'lsa → payload **FSM'ga park qilinadi** va darvoza
       ekrani ko'rsatiladi (Login / Register tugmalari bilan);
    3. hisob tasdiqlangan bo'lsa → payload darhol ijro etiladi.

    Park qilingan payload :func:`bot.handlers.auth.resume_after_auth` tomonidan
    login/register muvaffaqiyatli tugagach avtomatik ijro etiladi.
    """
    payload = (command.args if command else None) or ""
    payload = payload.strip()

    await state.clear()
    await _dispatch_start(message, payload, user, storage, share, tg_storage, settings, state)


async def _dispatch_start(
    message: Message,
    payload: str,
    user: User,
    storage: StorageService,
    share: ShareService,
    tg_storage: TelegramStorage,
    settings: Settings,
    state: FSMContext,
    *,
    force: bool = False,
) -> bool:
    """Deep-link grammatikasini ijro etish.

    :param force: ``True`` bo'lsa darvoza tekshirilmaydi (login'dan keyin
        replay paytida hisob allaqachon tasdiqlangan).
    :return: ``True`` = payload ijro etildi; ``False`` = darvoza ko'rsatildi.
    """
    if not force and not await _pass_gate_or_show(message, payload, user, storage, settings, state):
        return False

    if not payload:
        await _send_home(message, user, storage, settings)
        return True

    if payload.startswith(_PREFIX_REF):
        await _handle_referral(message, payload, user, storage)
        return True

    if payload.startswith(_PREFIX_FILE):
        await _open_own_file(message, payload[len(_PREFIX_FILE):], user, storage, tg_storage)
        return True

    if payload.startswith(_PREFIX_FOLDER):
        await _open_own_folder(message, payload[len(_PREFIX_FOLDER):], user, storage, settings)
        return True

    # Qolgan barcha payload'lar — ommaviy ulashish token'i (servis qatlami
    # egalik/ko'rinarlilikni qayta tekshiradi).
    await _open_share_token(message, payload, user, storage, share, tg_storage, settings, state)
    return True


async def _pass_gate_or_show(
    message: Message,
    payload: str,
    user: User,
    storage: StorageService,
    settings: Settings,
    state: FSMContext,
) -> bool:
    """Darvoza: o'tish mumkinmi? Aks holda park + ekran va ``False``."""
    from bot.middlewares.auth_guard import resolve_session_for
    from bot.session import SessionDecision, park_payload

    resolution = resolve_session_for(
        user,
        gate_enabled=settings.auth.required,
        is_admin=settings.bot.is_admin(user.telegram_id),
    )
    if resolution.decision is not SessionDecision.REQUIRE_AUTH:
        # Admin, tasdiqlangan hisob yoki o'chirilgan darvoza → davom etamiz.
        # (Bloklangan hisobni ``AuthGuardMiddleware`` ushlaydi.)
        return True

    # Deep-link yo'qolmasin: login/register tugagach avtomatik ijro etiladi.
    await park_payload(state, payload)
    await _show_gate(message, user, storage, settings, resolution, parked=bool(payload))
    return False


async def _show_gate(
    message: Message,
    user: User,
    storage: StorageService,
    settings: Settings,
    resolution: Any,
    *,
    parked: bool = False,
) -> None:
    """🚪 Darvoza ekrani: yangi va qaytib kelgan sessiya uchun alohida matn."""
    from bot.session import SessionKind

    if resolution.kind is SessionKind.NEW:
        text = texts.gate_new_session(
            settings.bot.username, settings.web.web_app_url or None, parked
        )
    else:
        text = texts.gate_returning_session(user.login, parked=parked)

    await message.answer(text, reply_markup=auth_reply_keyboard(user))
    logger.info(
        "Gate shown to %s (kind=%s parked=%s)", user.telegram_id, resolution.kind.value, parked
    )


async def _send_home(
    message: Message, user: User, storage: StorageService, settings: Settings
) -> None:
    """🏠 Bosh ekran — **Mini App birinchi** (Mini App Pivot talabi).

    Kirishdan keyin foydalanuvchi darhol ilovaga yo'naltiriladi; bot ichidagi
    menyular ikkinchi darajali zaxira yo'l sifatida qoladi.
    """
    stats = await storage.stats(user.telegram_id)
    # ``stats["files"]`` — butun son (``StorageService.stats`` shartnomasi).
    file_count = int(stats.get("files", 0) or 0)

    reply_keyboard = main_reply_keyboard(user, web_app_url=settings.web.web_app_url)
    inline_keyboard = mini_app_home_kb(web_app_url=settings.web.web_app_url)

    await message.answer(
        texts.home_welcome_authed(
            user.first_name or user.display_name,
            settings.web.web_app_url or None,
            file_count,
        ),
        reply_markup=reply_keyboard,
    )
    if file_count == 0:
        # Bo'sh bulut: birinchi faylni qanday yuklash aniq ko'rsatiladi.
        await message.answer(texts.home_first_file_hint(), reply_markup=inline_keyboard)
        return
    await message.answer(texts.dashboard(
        user,
        stats,
        bot_username=settings.bot.username,
        is_admin=settings.bot.is_admin(user.telegram_id),
    ), reply_markup=inline_keyboard)


async def resume_after_auth(
    message: Message,
    user: User,
    storage: StorageService,
    share: ShareService,
    tg_storage: TelegramStorage,
    settings: Settings,
    state: FSMContext,
) -> None:
    """Login/register muvaffaqiyatli tugagach chaqiriladi.

    Park qilingan deep-link bo'lsa — uzluksiz davom etadi (fayl yuboriladi /
    papka ochiladi). Bo'lmasa — Mini App bosh ekrani ko'rsatiladi.
    """
    from bot.session import take_parked_payload

    payload = await take_parked_payload(state)
    if payload:
        await message.answer(texts.session_resumed_deeplink())
        await _dispatch_start(
            message, payload, user, storage, share, tg_storage, settings, state, force=True
        )
        return

    await _send_home(message, user, storage, settings)


async def _send_dashboard(
    message: Message, user: User, storage: StorageService, settings: Settings
) -> None:
    """Render the dashboard: legacy main menu + quota summary.

    Two messages on purpose (as in the original bot): the menu with category tabs
    first, then the storage statistics line under it.
    """
    from bot.keyboards.legacy import main_menu_kb

    stats = await storage.stats(user.telegram_id)
    await message.answer(
        f"{Emoji.CLOUD} <b>Shaxsiy Bulut Xotirangiz</b>\n\n"
        f"{Emoji.UPLOAD} Fayl yuboring — avtomatik saqlanadi!\n"
        "Quyidagi menyudan tanlang:",
        reply_markup=main_menu_kb(user, web_app_url=settings.web.web_app_url),
    )
    await message.answer(
        texts.dashboard(
            user,
            stats,
            bot_username=settings.bot.username,
            is_admin=user.telegram_id in settings.bot.admin_ids,
        ),
        reply_markup=main_menu_keyboard(user, web_app_url=settings.web.web_app_url),
    )


async def _has_any_file(storage: StorageService, user: User) -> bool:
    """``True`` when the user already stored at least one file."""
    from sqlalchemy import func, select

    count = await storage.session.scalar(
        select(func.count()).select_from(File).where(File.user_id == user.telegram_id)
    )
    return bool(count)


async def _handle_referral(
    message: Message, payload: str, user: User, storage: StorageService
) -> None:
    """Store a referral source id in the user's settings (analytics only)."""
    inviter_raw = payload[len(_PREFIX_REF):]
    try:
        inviter_id = int(inviter_raw)
    except ValueError:
        await message.answer(texts.deep_link_not_found(payload))
        return

    if inviter_id != user.telegram_id and not user.setting("referred_by"):
        await storage.update_settings(user.telegram_id, {"referred_by": inviter_id})
        logger.info("User %s referred by %s", user.telegram_id, inviter_id)

    await message.answer(
        f"{texts.welcome(user, bot_username='bot')}\n\n"
        f"👋 You arrived by invitation link — your cloud is ready."
    )


async def _open_own_file(
    message: Message,
    hex_id: str,
    user: User,
    storage: StorageService,
    tg_storage: TelegramStorage,
) -> None:
    """Send one of the caller's own files (``?start=file_<uuid>``)."""
    try:
        record = await storage.get_file(_parse_uuid(hex_id), user.telegram_id)
    except (FileNotFoundError, ValueError):
        await message.answer(texts.deep_link_not_found(f"file_{hex_id}"))
        return

    await tg_storage.reply_with_file(
        message,
        record,
        caption=(
            f"☁️ <b>{record.file_name}</b>\n"
            f"📦 {record.file_size:,} bytes".replace(",", " ")
        ),
    )


async def _open_own_folder(
    message: Message,
    hex_id: str,
    user: User,
    storage: StorageService,
    settings: Settings,
) -> None:
    """Open the storage browser positioned on one folder."""
    from bot.handlers.storage import render_folder  # local import: avoids a cycle

    try:
        folder = await storage.get_folder(_parse_uuid(hex_id), user.telegram_id)
    except (FolderNotFoundError, ValueError):
        await message.answer(texts.deep_link_not_found(f"folder_{hex_id}"))
        return

    await render_folder(
        message=message,
        user=user,
        storage=storage,
        settings=settings,
        folder=folder,
        page=1,
    )


async def _open_share_token(
    message: Message,
    token: str,
    user: User,
    storage: StorageService,
    share: ShareService,
    tg_storage: TelegramStorage,
    settings: Settings,
    state: FSMContext,
) -> None:
    """Resolve a public share token: deliver the file and save a local copy.

    ``state`` majburiy: havola **parol bilan himoyalangan** bo'lsa
    :func:`~bot.handlers.share.start_pass_code_flow` FSM'ga token'ni yozib,
    foydalanuvchidan kod so'raydi. (Avval bu parametr yo'q edi va shu tarmoqda
    ``NameError: name 'state' is not defined`` yuzaga kelardi.)
    """
    try:
        record, link = await share.resolve_token(token)
    except LinkExpiredError:
        await message.answer(texts.link_expired_message())
        return
    except LinkRevokedError:
        await message.answer(texts.link_revoked_message())
        return
    except (LinkNotFoundError, FileNotFoundError):
        await message.answer(texts.link_not_found())
        return
    except InvalidPassCodeError:
        # Protected link: store the token in FSM and ask for the access code.
        from bot.handlers.share import start_pass_code_flow

        await start_pass_code_flow(message, token, state)
        return

    owner_name = record.owner.first_name if record.owner is not None else None

    # 1) Deliver the file (Telegram copies it server-side: instant, free).
    await tg_storage.deliver(
        message.chat.id,
        record,
        caption=(
            f"🔗 Shared file{f' from {owner_name}' if owner_name else ''}\n"
            f"Tap to download or play."
        ),
    )

    # 2) Save a copy into the recipient's own cloud when there is room.
    if record.user_id == user.telegram_id:
        await message.answer(texts.file_card(record))
        return

    try:
        stored = await storage.add_file(
            telegram_id=user.telegram_id,
            **record_to_kwargs(record),
        )
        copy = stored.record
        if not stored.is_new:
            # Dedupe: fayl allaqachon qabul qiluvchi bulutida bor.
            await message.answer(texts.dedupe_hit(record.file_name))
            return
    except DuplicateFileError:
        await message.answer(texts.dedupe_hit(record.file_name))
        return
    except (InsufficientQuotaError, FileNotFoundError):
        await message.answer(
            "☁️ The file was sent to you, but it did not fit into your quota — "
            "free space and use the deep link again."
        )
        return

    # Count the deep-link delivery as a download on both the link and the file.
    link.register_access(is_download=True)
    storage.session.add(link)
    await storage.session.commit()
    await message.answer(
        texts.deep_link_file_saved(copy, owner_name=owner_name),
        reply_markup=main_menu_keyboard(user, web_app_url=settings.web.web_app_url),
    )


def record_to_kwargs(record: File) -> dict[str, Any]:
    """Map an existing :class:`File` row onto :meth:`StorageService.add_file` args."""
    return {
        "file_id": record.file_id,
        "file_unique_id": record.file_unique_id,
        "file_name": record.file_name,
        "file_size": record.file_size,
        "mime_type": record.mime_type,
        "category": record.category,
        "caption": record.caption,
        "duration": record.duration,
        "width": record.width,
        "height": record.height,
        "thumbnail_file_id": record.thumbnail_file_id,
        "is_public": False,
    }


def _parse_uuid(value: str) -> uuid.UUID:
    """Accept both ``uuid.hex`` (deep links) and canonical UUID strings."""
    cleaned = value.strip()
    if len(cleaned) == 32 and all(char in "0123456789abcdefABCDEF" for char in cleaned):
        return uuid.UUID(cleaned)
    return uuid.UUID(cleaned)
