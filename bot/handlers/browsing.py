"""Legacy browsing mechanics + the 🔐 password gate handler.

This is the module that keeps the original bot's *feel*:

1. **Category tabs** — ``🎬 Videolar / 🖼️ Rasmlar / 🤖 APK/IPA / 📄 Boshqalar /
   📋 Barchasi / 📌 Muhimlar`` with ``cat:<key>:<page>`` callbacks.
2. **Files are sent as real media**, not as a text list: each row becomes a
   ``send_video`` / ``send_photo`` / ``send_document`` message with the legacy
   caption (``📌 🎬 <b>name</b>\\n💾 12.5 MB | 📅 date\\n📁 folder``) and its own
   action keyboard. Pagination buttons ride on the **last** file, exactly like
   before. ``BROWSE_SEND_MEDIA=false`` switches to the compact text list.
3. **📌 Pin / Unpin**, **📁 Move**, **🗑️ two-step delete** with the original
   callback grammar (ids are the 32-char UUID hex).
4. **Password gate** — :class:`~bot.states.PasswordState` handler with a
   constant-time comparison, attempt counter and cooldown.

Nothing is ever downloaded to disk: media are re-sent by ``file_id``.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot import texts
from bot.handlers import storage as storage_handlers
from bot.keyboards.legacy import (
    CAT_ALL,
    CAT_APPS,
    CAT_OTHER,
    CAT_PHOTO,
    CAT_PINNED,
    CAT_VIDEO,
    confirm_delete_kb,
    file_actions_kb,
    file_list_row,
    folders_kb,
    main_menu_kb,
    move_folders_kb,
    pagination_buttons,
)
from bot.keyboards.reply import cancel_reply_keyboard, main_reply_keyboard
from bot.states import PasswordState
from bot.utils.emojis import Emoji
from config import Settings
from db.models import File, FileCategory, Folder, User
from services.exceptions import (
    FileNotFoundError,
    FolderNotFoundError,
)
from services.storage import StorageService
from services.telegram import TelegramStorage

logger = logging.getLogger(__name__)

router = Router(name="browsing")

#: ``cat:<key>`` -> query filters understood by :meth:`StorageService.list_files`.
CATEGORY_FILTERS: dict[str, dict[str, Any]] = {
    CAT_VIDEO: {"category": FileCategory.VIDEO},
    CAT_PHOTO: {"category": FileCategory.PHOTO},
    CAT_APPS: {"apps_only": True},
    CAT_OTHER: {"category": (FileCategory.OTHER, FileCategory.DOCUMENT)},
    CAT_ALL: {},
    CAT_PINNED: {"pinned_only": True},
    "audio": {"category": (FileCategory.AUDIO, FileCategory.VOICE)},
    "document": {"category": FileCategory.DOCUMENT},
    "archive": {"category": FileCategory.ARCHIVE},
}


# ---------------------------------------------------------------------------
# 🔐 Password gate
# ---------------------------------------------------------------------------
@router.message(PasswordState.waiting_password, F.text)
async def on_password(
    message: Message,
    user: User,
    storage: StorageService,
    settings: Settings,
    state: FSMContext,
) -> None:
    """Verify the shared password (constant-time) and unlock the bot."""
    data = await state.get_data()
    attempts = int(data.get("password_attempts", 0))
    locked_until = float(data.get("password_locked_until", 0.0))

    if locked_until and time.time() < locked_until:
        await message.answer(texts.password_locked(int(locked_until - time.time()) + 1))
        return

    password = (message.text or "").strip()
    if password.lower() in {"cancel", "/cancel", "bekor"}:
        await state.clear()
        await message.answer(
            texts.operation_cancelled(),
            reply_markup=main_reply_keyboard(user, web_app_url=settings.web.web_app_url),
        )
        return

    if await storage.authorize_with_password(user.telegram_id, password):
        await state.clear()
        await message.answer(
            texts.password_ok(),
            reply_markup=main_reply_keyboard(user, web_app_url=settings.web.web_app_url),
        )
        await message.answer(
            texts.dashboard(
                user,
                await storage.stats(user.telegram_id),
                bot_username=settings.bot.username,
                is_admin=user.telegram_id in settings.bot.admin_ids,
            ),
            reply_markup=main_menu_kb(user, web_app_url=settings.web.web_app_url),
        )
        return

    attempts += 1
    max_attempts = settings.bot.password_max_attempts
    if attempts >= max_attempts:
        cooldown = settings.bot.password_cooldown
        await state.update_data(
            {"password_attempts": 0, "password_locked_until": time.time() + cooldown}
        )
        await message.answer(texts.password_locked(cooldown))
        return

    await state.update_data({"password_attempts": attempts})
    await message.answer(texts.password_wrong(max_attempts - attempts))


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "menu")
async def cb_menu(
    callback: CallbackQuery, user: User, settings: Settings
) -> None:
    """🏠 Menyu - the legacy main menu."""
    await _edit_or_send(
        callback,
        f"{Emoji.CLOUD} <b>Shaxsiy Bulut Xotirangiz</b>\n\nMenyudan tanlang:",
        main_menu_kb(user, web_app_url=settings.web.web_app_url),
    )
    await callback.answer()


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    """Page indicator - acknowledge without touching the message."""
    await callback.answer()


# ---------------------------------------------------------------------------
# Category tabs & folder browsing
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("cat:"))
async def cb_category(
    callback: CallbackQuery,
    user: User,
    storage: StorageService,
    settings: Settings,
    tg_storage: TelegramStorage,
) -> None:
    """``cat:<key>:<page>`` - one of the legacy category tabs."""
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    _, key, raw_page = parts
    if key not in CATEGORY_FILTERS:
        await callback.answer()
        return

    page = _safe_int(raw_page)
    await show_files_page(
        callback.message,
        context=f"cat:{key}",
        page=page,
        user=user,
        storage=storage,
        settings=settings,
        tg_storage=tg_storage,
        **CATEGORY_FILTERS[key],
    )
    await callback.answer()


@router.callback_query(F.data.startswith("folder:"))
async def cb_folder(
    callback: CallbackQuery,
    user: User,
    storage: StorageService,
    settings: Settings,
    tg_storage: TelegramStorage,
) -> None:
    """``folder:<uuid_hex|->:<page>`` - open one folder."""
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    _, folder_key, raw_page = parts
    page = _safe_int(raw_page)

    try:
        folder = await storage.get_folder_by_key(folder_key, user.telegram_id)
    except FolderNotFoundError:
        await callback.answer(texts.link_not_found(), show_alert=True)
        return

    await show_files_page(
        callback.message,
        context=f"folder:{folder_key}",
        page=page,
        user=user,
        storage=storage,
        settings=settings,
        tg_storage=tg_storage,
        folder=folder,
    )
    await callback.answer()


@router.callback_query(F.data == "folders")
async def cb_folders(
    callback: CallbackQuery, user: User, storage: StorageService
) -> None:
    """📁 Papkalar - the folder picker (``umumiy`` first, as before)."""
    folders = await storage.list_folders(user.telegram_id, parent_id=None)
    await _edit_or_send(
        callback,
        f"{Emoji.FOLDER} <b>Papkalar</b>\n\nPapkani tanlang:",
        folders_kb(folders),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# The core legacy renderer
# ---------------------------------------------------------------------------
async def show_files_page(
    message: Message,
    *,
    context: str,
    page: int,
    user: User,
    storage: StorageService,
    settings: Settings,
    tg_storage: TelegramStorage,
    category: FileCategory | tuple[FileCategory, ...] | None = None,
    apps_only: bool = False,
    pinned_only: bool = False,
    folder: Folder | None = None,
    search: str | None = None,
    header_key: str | None = None,
) -> None:
    """Render one page of files - as **real media** (legacy) or a text list.

    Media mode reproduces the original bot exactly:

    * the callback message is deleted and a header line is sent;
    * each file is sent with ``send_video/photo/document`` by ``file_id``
      (Telegram copies it server-side: no bandwidth, no disk);
    * the caption is ``📌 <icon> <b>name</b>\\n💾 MB | 📅 date\\n📁 folder``;
    * every file carries 📌/📁/🗑️ buttons and the **last** one also carries
      ``⬅️ 2/9 ➡️`` pagination plus 🏠 Menyu.
    """
    page_size = settings.storage.page_size
    items, total = await storage.list_files(
        user.telegram_id,
        folder_id=folder.id if folder else None,
        page=page + 1,          # service pages are 1-based, legacy pages 0-based
        page_size=page_size,
        search=search,
        category=category,
        apps_only=apps_only,
        pinned_only=pinned_only,
        include_folders=folder is not None or search is not None,
    )
    # Header title: explicit key wins, else derive it from the callback context.
    key = header_key
    if key is None:
        key = context.split(":", 1)[1] if context.startswith("cat:") else context
    if key not in CATEGORY_FILTERS and key not in texts.CATEGORY_TITLES:
        key = CAT_ALL
    header = texts.category_header(key, page, page_size, total)
    if folder is not None:
        header = f"{Emoji.FOLDER_OPEN} <b>{texts.escape_html(folder.breadcrumb())}</b> — {total} ta fayl:"

    if total == 0:
        await _delete_quietly(message)
        await message.answer(
            texts.no_files_yet(),
            reply_markup=main_menu_kb(user, web_app_url=settings.web.web_app_url),
        )
        return

    if not settings.storage.browse_send_media:
        await _render_text_list(
            message, items, total, page, page_size, context, user, settings, header
        )
        return

    await _delete_quietly(message)
    await message.answer(header)

    last_index = len(items) - 1
    for index, record in enumerate(items):
        caption = texts.file_caption(record, folder_name=_folder_name(record))
        keyboard = file_actions_kb(record.id, pinned=bool(record.is_pinned))

        if index == last_index:
            # Legacy detail: pagination + "Menyu" ride on the LAST file only.
            nav = pagination_buttons(context, page, total, page_size=page_size)
            if nav:
                keyboard.inline_keyboard.append(nav)
            keyboard.inline_keyboard.append(
                [_button(f"{Emoji.HOME.plain()} Menyu", "menu")]
            )

        await _send_media_safe(tg_storage, message.chat.id, record, caption, keyboard)


async def _render_text_list(
    message: Message,
    items: list[File] | Any,
    total: int,
    page: int,
    page_size: int,
    context: str,
    user: User,
    settings: Settings,
    header: str,
) -> None:
    """Compact alternative: one message with a button per file (saves requests)."""
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    builder = InlineKeyboardBuilder()
    for record in items:
        builder.row(file_list_row(record))
    nav = pagination_buttons(context, page, total, page_size=page_size)
    if nav:
        builder.row(*nav)
    builder.row(_button(f"{Emoji.HOME.plain()} Menyu", "menu"))

    await _edit_or_send(message, header, builder.as_markup())


# ---------------------------------------------------------------------------
# 📌 Pin / Unpin
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith(("pin:", "unpin:")))
async def cb_pin(
    callback: CallbackQuery,
    user: User,
    storage: StorageService,
) -> None:
    """Toggle the "muhim" flag and refresh only the keyboard (legacy behaviour)."""
    data = callback.data or ""
    _, _, file_key = data.partition(":")
    try:
        record = await storage.get_file_by_key(file_key, user.telegram_id)
    except (FileNotFoundError, ValueError):
        await callback.answer(texts.link_not_found(), show_alert=True)
        return

    want_pinned = data.startswith("pin:")
    if bool(record.is_pinned) != want_pinned:
        record = await storage.toggle_pin(record.id, user.telegram_id)

    try:
        await callback.message.edit_reply_markup(
            reply_markup=file_actions_kb(record.id, pinned=bool(record.is_pinned))
        )
    except TelegramBadRequest as exc:  # pragma: no cover - unchanged markup
        logger.debug("pin markup edit skipped: %s", exc)

    await callback.answer(texts.pin_toggled(record), show_alert=False, cache_time=3)


# ---------------------------------------------------------------------------
# 🗑️ Delete (two-step, legacy grammar)
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("delete:"))
async def cb_delete_ask(
    callback: CallbackQuery, user: User, storage: StorageService
) -> None:
    """Ask for confirmation before deleting."""
    _, _, file_key = (callback.data or "").partition(":")
    try:
        record = await storage.get_file_by_key(file_key, user.telegram_id)
    except (FileNotFoundError, ValueError):
        await callback.answer(texts.link_not_found(), show_alert=True)
        return

    await callback.message.answer(
        texts.confirm_delete_file(record), reply_markup=confirm_delete_kb(record.id)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("confirmdelete:"))
async def cb_delete_confirm(
    callback: CallbackQuery,
    user: User,
    storage: StorageService,
    settings: Settings,
) -> None:
    """Delete the file and free its quota."""
    _, _, file_key = (callback.data or "").partition(":")
    try:
        record = await storage.get_file_by_key(file_key, user.telegram_id)
    except (FileNotFoundError, ValueError):
        await callback.answer(texts.link_not_found(), show_alert=True)
        return

    freed = await storage.delete_file(record.id, user.telegram_id)
    await _edit_or_send(
        callback.message,
        texts.file_deleted(record, freed=freed),
        main_menu_kb(user, web_app_url=settings.web.web_app_url),
    )
    await callback.answer("O'chirildi!")


# ---------------------------------------------------------------------------
# 📁 Move
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("move:"))
async def cb_move_ask(
    callback: CallbackQuery, user: User, storage: StorageService
) -> None:
    """Show the destination picker."""
    _, _, file_key = (callback.data or "").partition(":")
    try:
        record = await storage.get_file_by_key(file_key, user.telegram_id)
    except (FileNotFoundError, ValueError):
        await callback.answer(texts.link_not_found(), show_alert=True)
        return

    folders = await storage.list_folders(user.telegram_id, parent_id=None)
    await callback.message.answer(
        texts.move_target_prompt(record, folder_count=len(folders) + 1),
        reply_markup=move_folders_kb(record.id, folders),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("domove:"))
async def cb_move_do(
    callback: CallbackQuery, user: User, storage: StorageService
) -> None:
    """``domove:<file>:<folder|->`` - perform the move."""
    parts = (callback.data or "").split(":", 2)
    if len(parts) != 3:
        await callback.answer()
        return
    _, file_key, folder_key = parts

    try:
        record = await storage.get_file_by_key(file_key, user.telegram_id)
        folder = await storage.get_folder_by_key(folder_key, user.telegram_id)
    except (FileNotFoundError, FolderNotFoundError, ValueError) as exc:
        await callback.answer(texts.error_generic(str(exc)), show_alert=True)
        return

    moved = await storage.move_file(record.id, user.telegram_id, folder.id if folder else None)
    await _edit_or_send(callback.message, texts.file_moved(moved, folder))
    await callback.answer()


# ---------------------------------------------------------------------------
# ➕ New folder / 🔍 Search / 📊 Stats
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "newfolder")
async def cb_new_folder(callback: CallbackQuery, state: FSMContext) -> None:
    """Start the folder-name flow."""
    await state.set_state(storage_handlers.NewFolderState.waiting_name)
    await state.update_data({"parent_folder": None})
    await callback.message.answer(texts.folder_new_prompt(), reply_markup=cancel_reply_keyboard())
    await callback.answer()


@router.callback_query(F.data == "search")
async def cb_search(callback: CallbackQuery, state: FSMContext) -> None:
    """Start the search flow (the next text message is the query)."""
    await state.set_state(storage_handlers.SearchState.waiting_query)
    await callback.message.answer(texts.search_prompt(), reply_markup=cancel_reply_keyboard())
    await callback.answer()


@router.callback_query(F.data == "stats")
async def cb_stats(
    callback: CallbackQuery, user: User, storage: StorageService, settings: Settings
) -> None:
    """📊 Statistika - quota, counters and per-category breakdown."""
    stats = await storage.stats(user.telegram_id)
    await _edit_or_send(
        callback,
        texts.stats_text(user, stats, max_file_size=settings.storage.max_file_size),
        main_menu_kb(user, web_app_url=settings.web.web_app_url),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Search results (legacy: send the actual files)
# ---------------------------------------------------------------------------
async def send_search_results(
    message: Message,
    *,
    query: str,
    user: User,
    storage: StorageService,
    settings: Settings,
    tg_storage: TelegramStorage,
) -> None:
    """Render search results as media (legacy) or as a button list."""
    # Bu yerda faqat **soni** kerak (ro'yxatni `show_files_page` qayta oladi).
    _items, total = await storage.list_files(
        user.telegram_id, search=query, page=1, page_size=settings.storage.page_size
    )
    if total == 0:
        await message.answer(
            texts.search_empty(),
            reply_markup=main_menu_kb(user, web_app_url=settings.web.web_app_url),
        )
        return

    await show_files_page(
        message,
        context="search",
        page=0,
        user=user,
        storage=storage,
        settings=settings,
        tg_storage=tg_storage,
        search=query,
        header_key=CAT_ALL,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _safe_int(value: str, default: int = 0) -> int:
    """Parse a page number from callback data without ever raising."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _folder_name(record: File) -> str:
    """Folder label for the legacy caption (``umumiy`` when at root)."""
    if record.folder is not None and record.folder.name:
        return record.folder.name
    return "umumiy"


async def _delete_quietly(message: Message) -> None:
    """Delete the callback message like the legacy bot did (ignore failures)."""
    try:
        await message.delete()
    except TelegramBadRequest as exc:  # pragma: no cover - not deletable
        logger.debug("Could not delete message %s: %s", message.message_id, exc)


async def _edit_or_send(
    target: Message | CallbackQuery, text: str, reply_markup: Any = None
) -> None:
    """Edit in place when possible, otherwise send a new message."""
    message = target.message if isinstance(target, CallbackQuery) else target
    if message is None:  # pragma: no cover
        return
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        # "message is not modified" / "can't edit" -> fall back to a new message.
        logger.debug("edit_text failed (%s), sending a new message", exc)
        chat_id = getattr(message, "chat", None)
        if chat_id is not None:
            await message.answer(text, reply_markup=reply_markup)


async def _send_media_safe(
    tg_storage: TelegramStorage,
    chat_id: int,
    record: File,
    caption: str,
    reply_markup: Any,
) -> None:
    """Legacy ``send_file_safe``: media first, text fallback on any failure."""
    try:
        await tg_storage.send_stored_file(
            chat_id,
            record.best_file_id,
            category=record.category,
            caption=caption,
            reply_markup=reply_markup,
            record=record,
        )
    except Exception as exc:
        logger.warning("Could not send %s as media (%s); falling back to text", record.id, exc)
        await tg_storage.bot.send_message(
            chat_id, caption, reply_markup=reply_markup, parse_mode="HTML"
        )


def _button(text: str, callback_data: str) -> Any:
    """Small factory keeping the legacy buttons in one place."""
    from aiogram.types import InlineKeyboardButton

    return InlineKeyboardButton(text=text, callback_data=callback_data)


def parse_legacy_uuid(value: str) -> uuid.UUID:
    """Public helper for tests: legacy callback id -> :class:`uuid.UUID`."""
    return uuid.UUID(value)
