"""🛡️ Admin panel — ``/diyor``.

Faqat ``ADMIN_ID`` (yoki ``ADMIN_IDS``) ro'yxatidagi Telegram akkaunti ochadi.
``ADMIN_PASSWORD`` sozlangan bo'lsa panel ochilishidan oldin parol so'raladi
(ikkinci qatlam: telefon kimning qo'lida bo'lishidan qat'i nazar).

Bo'limlar:

* 📊 **Tizim statistikasi** — foydalanuvchilar, faol sessiyalar, fayllar soni,
  indekslangan hajm, arxiv holati, top-10 foydalanuvchi;
* 👥 **Foydalanuvchilarni boshqarish** — sahifalangan ro'yxat, foydalanuvchi
  kartasi, uning fayllari, bloklash/blokdan chiqarish, parolni tiklash,
  sessiyani tugatish;
* 📢 **E'lon yuborish (broadcast)** — matn/rasm/video/hujjat barcha
  foydalanuvchiga ``copy_message`` orqali (formatlash saqlanadi), progress va
  yakuniy hisobot bilan;
* ⚙️ **Tizim sozlamalari** — autentifikatsiyani jonli yoqish/o'chirish,
  ro'yxatdan o'tishni ochish/yopish, arxiv kanalini tekshirish;
* 📜 **Audit jurnal** — so'nggi hodisalar (login, qulflash, bloklash, broadcast).

Barcha amallar ``audit_logs`` ga yoziladi, shuning uchun harakatlar tarixi
qayta tiklanadi.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot import admin_session, texts
from bot.keyboards.hugecloud import (
    admin_panel_kb,
    admin_settings_kb,
    admin_user_kb,
    admin_users_page_kb,
    audit_page_kb,
    user_row_buttons,
)
from bot.keyboards.reply import cancel_reply_keyboard
from bot.middlewares.auth_guard import set_auth_override
from bot.states import AdminState
from bot.utils.emojis import Emoji
from bot.utils.formatters import escape_html, format_size
from config import Settings
from db.models import AuditAction, User
from db.models.flag import FLAG_ALLOW_REGISTER, FLAG_AUTH_REQUIRED, set_flag
from services.audit import AuditService
from services.storage import StorageService
from services.vault import ArchiveVault, get_vault

logger = logging.getLogger(__name__)

router = Router(name="admin")

#: Bir sahifadagi foydalanuvchilar / audit yozuvlari soni.
PAGE_SIZE = 8
#: Broadcast progressini qancha qadamda yangilash.
BROADCAST_PROGRESS_EVERY = 20


# ---------------------------------------------------------------------------
# Yordamchilar
# ---------------------------------------------------------------------------
def _is_admin(telegram_id: int, settings: Settings) -> bool:
    """Panelga ruxsat bormi (yagona manba).

    Ikki yo'l: ``ADMIN_ID``/``ADMIN_IDS`` (doimiy) **yoki** ``/diyor`` da
    to'g'ri parol kiritilgan vaqtinchalik sessiya
    (:mod:`bot.admin_session`).
    """
    return settings.bot.is_admin(telegram_id) or admin_session.is_active(telegram_id)


def _pages(total: int, page_size: int = PAGE_SIZE) -> int:
    """Sahifalar soni (kamida 1)."""
    return max(1, -(-total // page_size))


def _flag_value(settings: Settings, key: str, default: bool) -> bool:
    """Jonli bayroq qiymati (runtime ustun ``.env`` dan ustun)."""
    if key == FLAG_AUTH_REQUIRED:
        from bot.middlewares.auth_guard import auth_override

        override = auth_override()
        return default if override is None else override
    return default


async def _persist_flag(
    storage: StorageService, key: str, value: bool, *, actor: int
) -> None:
    """Bayroqni ``system_flags`` jadvaliga yozish (restart'dan keyin ham amal qiladi)."""
    try:
        await set_flag(storage.session, key, value, updated_by=actor)
    except Exception:
        logger.debug("Could not persist flag %s", key, exc_info=True)
        await storage.session.rollback()


async def _vault(settings: Settings) -> ArchiveVault | None:
    """Arxiv xizmati (sozlanmagan bo'lsa ``None``)."""
    return get_vault(settings=settings)


# ---------------------------------------------------------------------------
# /diyor — panelni ochish
# ---------------------------------------------------------------------------
@router.message(Command("diyor"))
async def cmd_diyor(
    message: Message,
    user: User,
    settings: Settings,
    storage: StorageService,
    state: FSMContext,
) -> None:
    """``/diyor`` — boshqaruv panelini ochish (parol bilan).

    Buyruq ro'yxatida ko'rinmaydi va panel mavjudligi e'lon qilinmaydi:
    noto'g'ri foydalanuvchi shunchaki parol so'rovini ko'radi.
    """
    # 1) Doimiy admin yoki amal qiluvchi sessiya — to'g'ridan-to'g'ri.
    if settings.bot.is_admin(user.telegram_id) or admin_session.is_active(
        user.telegram_id
    ):
        await _open_panel(message, settings)
        return

    # 2) Parol sozlangan — so'raymiz.
    if settings.bot.admin_password_value():
        await state.set_state(AdminState.waiting_password)
        await message.answer(
            texts.admin_password_needed(), reply_markup=cancel_reply_keyboard(user)
        )
        return

    # 3) Parol ham, ID ham yo'q — panel sozlanmagan. Jimgina e'tiborsiz
    #    qoldiramiz, aks holda panel borligi oshkor bo'lardi.
    logger.warning(
        "Ignored /diyor from %s: panel is not configured", user.telegram_id
    )


@router.message(AdminState.waiting_password, F.text)
async def on_admin_password(
    message: Message,
    user: User,
    settings: Settings,
    storage: StorageService,
    state: FSMContext,
) -> None:
    """Admin parolini tekshirish (``hmac.compare_digest`` — timing hujumiga chidamli)."""
    import hmac

    entered = (message.text or "").strip()
    try:
        await message.delete()  # parol tarixda qolmasin
    except Exception:
        # Xabar o'chirilmasa ham parol tekshiruvi davom etadi (bot o'chmasin).
        logger.debug("Admin password message could not be deleted", exc_info=True)

    if entered.startswith("/") or entered in {"Cancel", "Bekor qilish"}:
        await state.clear()
        await message.answer(texts.operation_cancelled())
        return

    expected = settings.bot.admin_password_value()
    if not hmac.compare_digest(entered, expected):
        await AuditService(storage.session).safe_log(
            "admin_login_fail",
            actor_id=user.telegram_id,
            detail="wrong ADMIN_PASSWORD",
            success=False,
        )
        await message.answer(texts.admin_password_wrong(), reply_markup=cancel_reply_keyboard(user))
        return

    await state.clear()
    admin_session.grant(user.telegram_id)
    await AuditService(storage.session).safe_log(
        "admin_login_ok", actor_id=user.telegram_id, detail="/diyor opened"
    )
    await _open_panel(message, settings)


async def _open_panel(message: Message, settings: Settings) -> None:
    """Panel menyusini yuborish."""
    await message.answer(
        texts.admin_panel(settings.app.version), reply_markup=admin_panel_kb()
    )


# ---------------------------------------------------------------------------
# Callback: panel bo'limlari
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "adm")
async def cb_panel(callback: CallbackQuery, settings: Settings) -> None:
    """Panel menyusiga qaytish."""
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer(texts.admin_only(), show_alert=True)
        return
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.admin_panel(settings.app.version), reply_markup=admin_panel_kb()
    )
    await callback.answer()


@router.callback_query(F.data == "adm:close")
async def cb_close(callback: CallbackQuery, settings: Settings) -> None:
    """Panelni yopish (xabar o'chiriladi)."""
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer(texts.admin_only(), show_alert=True)
        return
    try:
        await callback.message.delete()  # type: ignore[union-attr]
    except TelegramAPIError:
        await callback.message.edit_text(f"{Emoji.CHECK.plain()} Yopildi")  # type: ignore[union-attr]
    await callback.answer()


@router.callback_query(F.data == "adm:stats")
async def cb_stats(
    callback: CallbackQuery,
    settings: Settings,
    storage: StorageService,
) -> None:
    """📊 Tizim statistikasi."""
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer(texts.admin_only(), show_alert=True)
        return

    stats = await storage.admin_stats()
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.admin_stats(stats), reply_markup=admin_panel_kb()
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# 👥 Foydalanuvchilar
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("adm:users:"))
async def cb_users(
    callback: CallbackQuery,
    settings: Settings,
    storage: StorageService,
    state: FSMContext,
) -> None:
    """Sahifalangan foydalanuvchilar ro'yxati."""
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer(texts.admin_only(), show_alert=True)
        return

    page = _int_part(callback.data, 2)
    data = await state.get_data()
    search = data.get("admin_user_search")

    rows, total = await storage.list_users(
        page=page + 1, page_size=PAGE_SIZE, search=search
    )
    if not rows:
        await callback.message.edit_text(  # type: ignore[union-attr]
            texts.admin_no_users(), reply_markup=admin_panel_kb()
        )
        await callback.answer()
        return

    builder = admin_users_page_kb(page, _pages(total))
    for row in user_row_buttons(rows):
        builder.inline_keyboard.append(row)
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.admin_users_page(rows, page, _pages(total), total),
        reply_markup=builder,
    )
    await callback.answer()


@router.callback_query(F.data == "adm:usearch")
async def cb_user_search(
    callback: CallbackQuery, settings: Settings, state: FSMContext
) -> None:
    """Foydalanuvchi qidirish: ID yoki login yuborishni so'rash."""
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer(texts.admin_only(), show_alert=True)
        return
    await state.set_state(AdminState.waiting_user_id)
    await callback.message.answer(  # type: ignore[union-attr]
        f"{Emoji.SEARCH.plain()} Foydalanuvchi ID yoki loginini yuboring "
        "(qidiruv ro'yxatni ham filtrlashi mumkin):",
        reply_markup=cancel_reply_keyboard(),
    )
    await callback.answer()


@router.message(AdminState.waiting_user_id, F.text)
async def on_user_search(
    message: Message,
    settings: Settings,
    storage: StorageService,
    state: FSMContext,
) -> None:
    """ID/login bo'yicha foydalanuvchini topish."""
    if not _is_admin(message.from_user.id, settings):  # type: ignore[union-attr]
        await message.answer(texts.admin_only())
        return

    value = (message.text or "").strip()
    if not value or value.startswith("/"):
        await state.clear()
        await message.answer(texts.operation_cancelled())
        return

    await state.clear()
    target: User | None = None
    if value.lstrip("-").isdigit():
        target = await storage.get_user_any(int(value))
    if target is None:
        rows, _ = await storage.list_users(page=1, page_size=1, search=value)
        target = rows[0] if rows else None

    if target is None:
        await message.answer(texts.admin_user_not_found(escape_html(value)))
        return

    files, size = await storage.user_files_count(target.telegram_id)
    await message.answer(
        texts.admin_user_card(target, files, size, storage.quota_label(target)),
        reply_markup=admin_user_kb(target),
    )


@router.callback_query(F.data.startswith("adm:u:"))
async def cb_user_card(
    callback: CallbackQuery,
    settings: Settings,
    storage: StorageService,
) -> None:
    """Bitta foydalanuvchi kartasi."""
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer(texts.admin_only(), show_alert=True)
        return

    telegram_id = _int_part(callback.data, 2)
    target = await storage.get_user_any(telegram_id)
    if target is None:
        await callback.answer(texts.admin_user_not_found(telegram_id), show_alert=True)
        return

    files, size = await storage.user_files_count(telegram_id)
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.admin_user_card(target, files, size, storage.quota_label(target)),
        reply_markup=admin_user_kb(target),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:block:"))
async def cb_block(
    callback: CallbackQuery,
    settings: Settings,
    storage: StorageService,
) -> None:
    """🚫 Foydalanuvchini bloklash."""
    await _toggle_block(callback, settings, storage, blocked=True)


@router.callback_query(F.data.startswith("adm:unblock:"))
async def cb_unblock(
    callback: CallbackQuery,
    settings: Settings,
    storage: StorageService,
) -> None:
    """✅ Blokdan chiqarish."""
    await _toggle_block(callback, settings, storage, blocked=False)


async def _toggle_block(
    callback: CallbackQuery,
    settings: Settings,
    storage: StorageService,
    *,
    blocked: bool,
) -> None:
    """Bloklash/blokdan chiqarish + audit + foydalanuvchiga xabar."""
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer(texts.admin_only(), show_alert=True)
        return

    telegram_id = _int_part(callback.data, 2)
    target = await storage.get_user_any(telegram_id)
    if target is None:
        await callback.answer(texts.admin_user_not_found(telegram_id), show_alert=True)
        return

    await storage.set_blocked(telegram_id, blocked, note="admin panel")
    await AuditService(storage.session).safe_log(
        AuditAction.ADMIN_BLOCK if blocked else AuditAction.ADMIN_UNBLOCK,
        actor_id=callback.from_user.id,
        target_id=telegram_id,
        detail=f"{'blocked' if blocked else 'unblocked'} via /diyor",
    )

    # Foydalanuvchining o'ziga ham xabar beramiz (yetib borishi shart emas).
    try:
        await callback.bot.send_message(
            telegram_id, texts.blocked_message("administrator qarori") if blocked else texts.logout_ok()
        )
    except TelegramAPIError:
        logger.debug("Could not notify %s about the block change", telegram_id)

    files, size = await storage.user_files_count(telegram_id)
    await callback.message.edit_text(  # type: ignore[union-attr]
        (
            texts.admin_blocked(target.display_name)
            if blocked
            else texts.admin_unblocked(target.display_name)
        )
        + "\n\n"
        + texts.admin_user_card(target, files, size, storage.quota_label(target)),
        reply_markup=admin_user_kb(target),
    )
    await callback.answer("✅" if not blocked else "🚫")


@router.callback_query(F.data.startswith("adm:resetpw:"))
async def cb_reset_password(
    callback: CallbackQuery,
    settings: Settings,
    storage: StorageService,
) -> None:
    """🔑 Parolni tiklash: tasodifiy parol generatsiya qilinadi."""
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer(texts.admin_only(), show_alert=True)
        return

    telegram_id = _int_part(callback.data, 2)
    target = await storage.get_user_any(telegram_id)
    if target is None:
        await callback.answer(texts.admin_user_not_found(telegram_id), show_alert=True)
        return

    target, password = await storage.reset_password(telegram_id)
    await AuditService(storage.session).safe_log(
        AuditAction.PASSWORD_RESET,
        actor_id=callback.from_user.id,
        target_id=telegram_id,
        detail="password reset via /diyor",
    )
    await callback.message.answer(  # type: ignore[union-attr]
        texts.admin_password_reset(target.display_name, password)
    )
    await callback.answer("Parol yaratildi")


@router.callback_query(F.data.startswith("adm:logout:"))
async def cb_force_logout(
    callback: CallbackQuery,
    settings: Settings,
    storage: StorageService,
) -> None:
    """⏳ Foydalanuvchi sessiyasini majburan tugatish."""
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer(texts.admin_only(), show_alert=True)
        return

    telegram_id = _int_part(callback.data, 2)
    target = await storage.get_user_any(telegram_id)
    if target is None:
        await callback.answer(texts.admin_user_not_found(telegram_id), show_alert=True)
        return

    from services.auth import AuthService

    await AuthService(storage.session, storage=storage, settings=settings).logout(telegram_id)
    await AuditService(storage.session).safe_log(
        "admin_force_logout",
        actor_id=callback.from_user.id,
        target_id=telegram_id,
        detail="session terminated via /diyor",
    )
    await callback.answer("Sessiya tugatildi", show_alert=True)


@router.callback_query(F.data.startswith("adm:userfiles:"))
async def cb_user_files(
    callback: CallbackQuery,
    settings: Settings,
    storage: StorageService,
) -> None:
    """🗃️ Tanlangan foydalanuvchi fayllari (admin ko'rinishi)."""
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer(texts.admin_only(), show_alert=True)
        return

    parts = callback.data.split(":")
    telegram_id = int(parts[2]) if len(parts) > 2 and parts[2].lstrip("-").isdigit() else 0
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0

    rows, total = await storage.list_files(
        telegram_id, page=page + 1, page_size=PAGE_SIZE, sort_by="created_at", sort_desc=True
    )
    
    from aiogram.types import InputMediaPhoto
    media_group = []
    text_lines = []

    for record in rows:
        if record.thumbnail_file_id:
            caption = f"{Emoji.for_category(record.category)} {escape_html(record.file_name[:32])} · {format_size(record.file_size)}"
            media_group.append(InputMediaPhoto(media=record.thumbnail_file_id, caption=caption))
        else:
            text_lines.append(
                f"{Emoji.for_category(record.category)} <code>{record.id.hex[:8]}</code> "
                f"{escape_html(record.file_name[:40])} · {format_size(record.file_size)}"
                + ("  🛡️" if record.is_archived else "")
            )

    if media_group:
        try:
            if hasattr(callback.message, "answer_media_group"):
                if len(media_group) == 1:
                    await callback.message.answer_photo(
                        photo=media_group[0].media,
                        caption=media_group[0].caption,
                    )
                else:
                    await callback.message.answer_media_group(media_group[:10])
            else:
                if len(media_group) == 1:
                    await callback.bot.send_photo(
                        chat_id=callback.message.chat.id,
                        photo=media_group[0].media,
                        caption=media_group[0].caption,
                    )
                else:
                    await callback.bot.send_media_group(
                        chat_id=callback.message.chat.id,
                        media=media_group[:10],
                    )
        except Exception:
            text_lines = [
                f"{Emoji.for_category(record.category)} <code>{record.id.hex[:8]}</code> "
                f"{escape_html(record.file_name[:40])} · {format_size(record.file_size)}"
                + ("  🛡️" if record.is_archived else "")
                for record in rows
            ]
            
    if not text_lines and not rows:
        text_lines = [texts.no_files_yet()]

    nav: list[Any] = []
    pages = _pages(total)
    if pages > 1:
        if page > 0:
            nav.append(_inline("⬅️", f"adm:userfiles:{telegram_id}:{page - 1}"))
        nav.append(_inline(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(_inline("➡️", f"adm:userfiles:{telegram_id}:{page + 1}"))

    markup = admin_users_page_kb(page, pages)
    if nav:
        markup.inline_keyboard.insert(0, nav)
    text_body = "\n".join(text_lines) if text_lines else "<i>Barcha fayllar prevyuda ko'rsatildi</i>"
    await callback.message.edit_text(  # type: ignore[union-attr]
        f"🗃️ <b>ID {telegram_id}</b> fayllari ({total} ta):\n\n{text_body}",
        reply_markup=markup,
    )
    await callback.answer()


def _inline(text: str, data: str) -> Any:
    """InlineKeyboardButton qisqartmasi."""
    from aiogram.types import InlineKeyboardButton

    return InlineKeyboardButton(text=text[:64], callback_data=data[:64])


# ---------------------------------------------------------------------------
# 📢 Broadcast
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "adm:bcast")
async def cb_broadcast(
    callback: CallbackQuery,
    settings: Settings,
    storage: StorageService,
    state: FSMContext,
) -> None:
    """E'lon rejimini boshlash: administrator xabar yuborishini kutamiz."""
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer(texts.admin_only(), show_alert=True)
        return

    targets = await storage.broadcast_targets()
    await state.set_state(AdminState.waiting_broadcast)
    await callback.message.answer(  # type: ignore[union-attr]
        texts.admin_broadcast_prompt(len(targets)), reply_markup=cancel_reply_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data == "adm:bcast:dry")
async def cb_broadcast_dry(
    callback: CallbackQuery,
    settings: Settings,
    storage: StorageService,
) -> None:
    """Quruq broadcast: hech narsa yubormasdan auditoriya hajmini ko'rsatadi."""
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer(texts.admin_only(), show_alert=True)
        return
    targets = await storage.broadcast_targets()
    blocked = len(await storage.broadcast_targets(include_blocked=True)) - len(targets)
    await callback.answer(
        f"Auditoriya: {len(targets)} ta (bloklangan {blocked} ta tashlab yuborildi)",
        show_alert=True,
    )


@router.message(AdminState.waiting_broadcast)
async def on_broadcast(
    message: Message,
    settings: Settings,
    storage: StorageService,
    state: FSMContext,
) -> None:
    """Yuborilgan xabarni barcha foydalanuvchiga yetkazish.

    ``copy_message`` ishlatiladi: formatlash, media, tugmalar (ba'zilari) saqlanadi
    va fayl Telegram serverida qoladi — hech qanday yuklab olish/yuklash yo'q.
    Telegram ``429 retry_after`` javobini bersa, kutib turib qayta urinamiz.
    """
    if not _is_admin(message.from_user.id, settings):  # type: ignore[union-attr]
        await message.answer(texts.admin_only())
        return

    raw = (message.text or "").strip()
    if message.text and (raw.startswith("/") or raw in {"Cancel", "Bekor qilish"}):
        await state.clear()
        await message.answer(texts.operation_cancelled())
        return

    targets = await storage.broadcast_targets()
    await state.clear()

    progress = await message.answer(texts.admin_broadcast_progress(0, 0, len(targets)))
    started = time.monotonic()
    sent = 0
    failed = 0

    for index, chat_id in enumerate(targets, start=1):
        try:
            await message.copy_to(chat_id)
            sent += 1
        except TelegramRetryAfter as exc:
            # Flood limit: ko'rsatilgan vaqtni kutib, bir marta qayta urinamiz.
            await asyncio.sleep(min(exc.retry_after, 30))
            try:
                await message.copy_to(chat_id)
                sent += 1
            except TelegramAPIError:
                failed += 1
        except TelegramForbiddenError:
            # Foydalanuvchi botni bloklagan — belgilab qo'yamiz.
            failed += 1
            await _mark_blocked(storage, chat_id)
        except TelegramAPIError as exc:
            failed += 1
            logger.info("Broadcast to %s failed: %s", chat_id, exc)

        if index % BROADCAST_PROGRESS_EVERY == 0:
            try:
                await progress.edit_text(
                    texts.admin_broadcast_progress(sent, failed, len(targets))
                )
            except TelegramAPIError:
                pass
        # Telegram'ning ~30 xabar/soniya chegarasiga yaqinlashmaslik uchun.
        await asyncio.sleep(0.05)

    elapsed = time.monotonic() - started
    try:
        await progress.edit_text(
            texts.admin_broadcast_done(sent, failed, len(targets), elapsed)
        )
    except TelegramAPIError:
        await message.answer(texts.admin_broadcast_done(sent, failed, len(targets), elapsed))

    await AuditService(storage.session).safe_log(
        AuditAction.BROADCAST,
        actor_id=message.from_user.id,  # type: ignore[union-attr]
        detail=f"broadcast: {sent}/{len(targets)} delivered, {failed} failed",
        extra={"sent": sent, "failed": failed, "total": len(targets), "seconds": round(elapsed, 2)},
    )


async def _mark_blocked(storage: StorageService, telegram_id: int) -> None:
    """Botni bloklagan foydalanuvchini belgilash (keyingi broadcast'larda skip)."""
    try:
        user = await storage.get_user_any(telegram_id)
        if user is not None and not user.is_blocked:
            user.is_blocked = True
            storage.session.add(user)
            await storage.session.commit()
    except Exception:
        logger.debug("Could not mark %s as blocked", telegram_id, exc_info=True)
        await storage.session.rollback()


# ---------------------------------------------------------------------------
# ⚙️ Tizim sozlamalari
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "adm:settings")
async def cb_settings(callback: CallbackQuery, settings: Settings, storage: StorageService) -> None:
    """Sozlamalar ekrani (jonli qiymatlar bilan)."""
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer(texts.admin_only(), show_alert=True)
        return

    auth_required = _flag_value(settings, FLAG_AUTH_REQUIRED, settings.auth.required)
    vault = await _vault(settings)
    archive_line = texts.archive_status_line(
        (await vault.status()).as_line() if vault else "❌ arxiv xizmati ishga tushmagan"
    )
    quota_label = (
        settings.storage.unlimited_label
        if settings.storage.default_quota <= 0
        else format_size(settings.storage.default_quota)
    )
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.admin_settings(
            auth_required,
            settings.auth.allow_registration,
            archive_line,
            quota_label,
        ),
        reply_markup=admin_settings_kb(
            auth_required=auth_required, allow_register=settings.auth.allow_registration
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "adm:toggle:auth")
async def cb_toggle_auth(
    callback: CallbackQuery, settings: Settings, storage: StorageService
) -> None:
    """Autentifikatsiyani yoqish/o'chirish (qayta ishga tushirmasdan)."""
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer(texts.admin_only(), show_alert=True)
        return

    current = _flag_value(settings, FLAG_AUTH_REQUIRED, settings.auth.required)
    new_value = not current
    set_auth_override(new_value)
    await _persist_flag(storage, FLAG_AUTH_REQUIRED, new_value, actor=callback.from_user.id)
    await AuditService(storage.session).safe_log(
        AuditAction.ADMIN_AUTH_TOGGLE,
        actor_id=callback.from_user.id,
        detail=f"AUTH_REQUIRED -> {new_value}",
        extra={"value": new_value},
    )
    await callback.answer(texts.admin_toggled("Autentifikatsiya", new_value), show_alert=True)
    await cb_settings(callback, settings, storage)


@router.callback_query(F.data == "adm:toggle:register")
async def cb_toggle_register(
    callback: CallbackQuery, settings: Settings, storage: StorageService
) -> None:
    """Ro'yxatdan o'tishni ochish/yopish."""
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer(texts.admin_only(), show_alert=True)
        return

    new_value = not settings.auth.allow_registration
    settings.auth.allow_registration = new_value  # jonli (in-memory) o'zgarish
    await _persist_flag(storage, FLAG_ALLOW_REGISTER, new_value, actor=callback.from_user.id)
    await AuditService(storage.session).safe_log(
        "admin_register_toggle",
        actor_id=callback.from_user.id,
        detail=f"AUTH_ALLOW_REGISTER -> {new_value}",
        extra={"value": new_value},
    )
    await callback.answer(texts.admin_toggled("Ro'yxatdan o'tish", new_value), show_alert=True)
    await cb_settings(callback, settings, storage)


@router.callback_query(F.data == "adm:archivestat")
async def cb_archive_status(callback: CallbackQuery, settings: Settings) -> None:
    """🛡️ Arxiv kanalini tekshirish (chat, admin huquqi, hisoblagichlar)."""
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer(texts.admin_only(), show_alert=True)
        return

    vault = get_vault(settings=settings)
    if vault is None:
        await callback.answer("Arxiv xizmati ishga tushmagan", show_alert=True)
        return

    status = await vault.status()
    await callback.message.answer(  # type: ignore[union-attr]
        f"🛡️ <b>Arxiv holati</b>\n\n"
        f"Kanal: <code>{escape_html(status.channel_id)}</code>\n"
        f"Sarlavha: {escape_html(status.chat_title or '—')}\n"
        f"Tur: {status.chat_type or '—'}\n"
        f"Bot admin: {'✅' if status.bot_is_admin else '❌'}\n"
        f"Yuborish huquqi: {'✅' if status.bot_can_post else '❌'}\n"
        f"Nusxalangan fayllar: <b>{status.archived_files}</b>\n"
        f"Kutilayotgan: <b>{status.pending_files}</b>\n"
        f"Xato: {escape_html(status.error or '—')}\n\n"
        f"{texts.archive_status_line(status.as_line())}",
        reply_markup=admin_settings_kb(
            auth_required=settings.auth.required,
            allow_register=settings.auth.allow_registration,
        ),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# 📜 Audit jurnal
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("adm:audit:"))
async def cb_audit(
    callback: CallbackQuery, settings: Settings, storage: StorageService
) -> None:
    """So'nggi audit yozuvlari."""
    await _render_audit(callback, settings, storage, only_failed=False)


@router.callback_query(F.data.startswith("adm:auditf:"))
async def cb_audit_failed(
    callback: CallbackQuery, settings: Settings, storage: StorageService
) -> None:
    """Faqat muvaffaqiyatsiz hodisalar."""
    await _render_audit(callback, settings, storage, only_failed=True)


async def _render_audit(
    callback: CallbackQuery,
    settings: Settings,
    storage: StorageService,
    *,
    only_failed: bool,
) -> None:
    """Audit sahifasini chizish (ikkala callback uchun umumiy)."""
    if not _is_admin(callback.from_user.id, settings):
        await callback.answer(texts.admin_only(), show_alert=True)
        return

    page = _int_part(callback.data, 2)
    rows, total = await storage.recent_audit(
        limit=PAGE_SIZE, only_failed=only_failed
    )
    # Sahifalash: recent_audit limit beradi, offset uchun alohida so'rov.
    if page:
        rows, total = await _audit_page(storage, page, only_failed)

    pages = _pages(total)
    prefix = "adm:auditf" if only_failed else "adm:audit"
    markup = audit_page_kb(page, pages)
    # "Faqat xatolar" tugmasini joriy rejimga moslaymiz.
    for row in markup.inline_keyboard:
        for button in row:
            if button.callback_data and button.callback_data.startswith("adm:auditf"):
                button.callback_data = f"{prefix}:{page}"
                button.text = "🚫 Faqat xatolar" if only_failed else "❌ Faqat xatolar"

    body = "\n".join(_audit_line(row) for row in rows) or "Hozircha yozuv yo'q."
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.admin_audit_page(list(rows), page, pages)
        if rows
        else f"📜 <b>Audit jurnal</b>\n\n{body}",
        reply_markup=markup,
    )
    await callback.answer()


async def _audit_page(
    storage: StorageService, page: int, only_failed: bool
) -> tuple[Sequence[Any], int]:
    """Audit jurnalining aniq sahifasi (offset bilan)."""
    from sqlalchemy import desc, func, select

    from db.models import AuditLog

    conditions = [AuditLog.success.is_(False)] if only_failed else []
    statement = select(AuditLog).order_by(desc(AuditLog.created_at), desc(AuditLog.id))
    count_statement = select(func.count()).select_from(AuditLog)
    for condition in conditions:
        statement = statement.where(condition)
        count_statement = count_statement.where(condition)
    statement = statement.limit(PAGE_SIZE).offset(page * PAGE_SIZE)

    rows = (await storage.session.scalars(statement)).all()
    total = int(await storage.session.scalar(count_statement) or 0)
    return rows, total


def _audit_line(entry: Any) -> str:
    """Bitta audit yozuvi matni."""
    from bot.utils.formatters import format_date

    icon = "✅" if entry.success else "❌"
    when = format_date(entry.created_at, fmt="%m-%d %H:%M")
    detail = (entry.detail or "")[:60]
    actor = entry.actor_id or "—"
    return f"{icon} <code>{when}</code> · {entry.action} · ID {actor} · {escape_html(detail)}"


# ---------------------------------------------------------------------------
# Umumiy
# ---------------------------------------------------------------------------
def _int_part(data: str, index: int, default: int = 0) -> int:
    """Callback data'dan butun son olish (xato bo'lsa ``default``)."""
    parts = data.split(":")
    if len(parts) <= index:
        return default
    value = parts[index]
    try:
        return int(value)
    except ValueError:
        return default
