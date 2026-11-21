"""Callback-query router: every inline button press lands here.

Registered **first** in the dispatcher, so callback traffic never reaches the
message handlers. Each branch keeps the same contract:

1. resolve the target row through a service (ownership checked there);
2. perform the mutation or build the next screen;
3. ``edit_text`` in place (falling back to ``answer`` when the message is too
   old to edit) and always ``callback.answer()`` so the button spinner stops.

Destructive actions use a two-step confirmation
(:func:`bot.handlers.storage.confirm_delete_file`).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from bot import texts
from bot.handlers import storage as storage_handlers
from bot.handlers.share import render_share_menu
from bot.keyboards.inline import (
    ConfirmFactory,
    FileActionFactory,
    NavFactory,
    SettingsFactory,
    main_menu_keyboard,
    settings_keyboard,
    to_uuid,
    uid_of,
)
from bot.utils.emojis import Emoji
from bot.utils.formatters import escape_html, format_size, truncate_middle
from config import Settings
from db.models import File, Folder, User
from services.exceptions import (
    FileNotFoundError,
    FolderNotFoundError,
    NotOwnerError,
)
from services.share import ShareService
from services.storage import StorageService
from services.telegram import TelegramStorage

logger = logging.getLogger(__name__)

router = Router(name="callbacks")


# ---------------------------------------------------------------------------
# Small helpers shared by every branch
# ---------------------------------------------------------------------------
async def _edit(callback: CallbackQuery, text: str, reply_markup: Any = None) -> None:
    """Edit the callback's message, falling back to a new message when needed."""
    message = callback.message
    if isinstance(message, Message):
        try:
            await message.edit_text(text, reply_markup=reply_markup)
        except TelegramBadRequest as exc:
            # "message is not modified" is the common, harmless case.
            logger.debug("edit_text skipped: %s", exc)
    else:  # pragma: no cover - inline messages we cannot edit
        await callback.message.answer(text, reply_markup=reply_markup)  # type: ignore[union-attr]


async def _alert(callback: CallbackQuery, text: str) -> None:
    """Show a modal alert (used for errors that must be acknowledged)."""
    try:
        await callback.answer(text=text[:200], show_alert=True, cache_time=5)
    except Exception:  # pragma: no cover - stale callback id
        logger.debug("callback.answer(alert) failed")


async def _load_file(
    storage: StorageService, uid: str, telegram_id: int
) -> File | None:
    """Resolve a compact uid into an owned :class:`File` (``None`` on failure)."""
    try:
        return await storage.get_file(to_uuid(uid), telegram_id)
    except (FileNotFoundError, ValueError):
        return None


async def _load_folder(
    storage: StorageService, uid: str, telegram_id: int
) -> Folder | None:
    """Resolve a compact uid into an owned :class:`Folder` (``None`` == root)."""
    if not uid or uid == "-":
        return None
    try:
        return await storage.get_folder(to_uuid(uid), telegram_id)
    except (FolderNotFoundError, ValueError):
        return None


async def render_file_card(
    callback: CallbackQuery,
    *,
    user: User,
    storage: StorageService,
    record: File,
) -> None:
    """Render the single-file screen (text + action keyboard)."""
    from bot.keyboards.inline import file_actions_keyboard

    await _edit(
        callback,
        texts.file_card(record, folder=record.folder, links=record.shared_links),
        file_actions_keyboard(record, user=user, folder=record.folder, links=record.shared_links),
    )


async def render_settings(
    *,
    message: Message | None = None,
    callback: CallbackQuery | None = None,
    user: User,
    settings: Settings,
    edit: bool = True,
) -> None:
    """Render the preferences screen from either entry point."""
    del settings  # toggles come from the user row itself
    rows = {
        "Publish new files by default": bool(user.setting("public_by_default", False)),
        "Inline search in any chat": bool(user.setting("inline_enabled", True)),
        "Confirm every upload": bool(user.setting("notify_uploads", True)),
    }
    text = texts.settings_text(user, rows)
    keyboard = settings_keyboard(user)

    if callback is not None and edit:
        await _edit(callback, text, keyboard)
        await callback.answer()
        return
    target = message or (callback.message if callback else None)
    if isinstance(target, Message):
        await target.answer(text, reply_markup=keyboard)


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------
@router.callback_query(NavFactory.filter())
async def on_navigate(
    callback: CallbackQuery,
    callback_data: NavFactory,
    user: User,
    storage: StorageService,
    settings: Settings,
    state: FSMContext,
) -> None:
    """Move between folders / pages / search results."""
    await state.clear()
    folder = await _load_folder(storage, callback_data.folder, user.telegram_id)
    query = None if callback_data.q in ("-", "") else callback_data.q

    await storage_handlers.render_folder(
        callback=callback,
        user=user,
        storage=storage,
        settings=settings,
        folder=folder,
        page=callback_data.page,
        query=query,
    )
    await state.update_data({storage_handlers.DATA_FOLDER: callback_data.folder})


@router.callback_query(F.data == "home")
async def on_home(
    callback: CallbackQuery, user: User, storage: StorageService, settings: Settings, state: FSMContext
) -> None:
    """Back to the dashboard."""
    await state.clear()
    stats = await storage.stats(user.telegram_id)
    await _edit(
        callback,
        texts.dashboard(
            user,
            stats,
            bot_username=settings.bot.username,
            is_admin=user.telegram_id in settings.bot.admin_ids,
        ),
        main_menu_keyboard(user, web_app_url=settings.web.web_app_url),
    )
    await callback.answer()


@router.callback_query(F.data == "stats")
async def on_stats(callback: CallbackQuery, user: User, storage: StorageService, settings: Settings) -> None:
    """Quota + per-category statistics."""
    stats = await storage.stats(user.telegram_id)
    await _edit(
        callback, texts.stats_text(user, stats, max_file_size=settings.storage.max_file_size)
    )
    await callback.answer()


@router.callback_query(F.data == "search")
async def on_search_button(callback: CallbackQuery, state: FSMContext) -> None:
    """Switch the chat into "waiting for a search query" mode."""
    await state.set_state(storage_handlers.SearchState.waiting_query)
    await _edit(callback, texts.search_prompt())
    await callback.answer("Type the file name in the chat", show_alert=False)


@router.callback_query(F.data == "settings")
async def on_settings_button(callback: CallbackQuery, user: User, settings: Settings) -> None:
    """Open preferences."""
    await render_settings(callback=callback, user=user, settings=settings)


@router.callback_query(F.data == "inline_help")
async def on_inline_help(callback: CallbackQuery, settings: Settings) -> None:
    """Explain inline mode."""
    await _edit(callback, texts.inline_hint(bot_username=settings.bot.username))
    await callback.answer()


@router.callback_query(F.data == "noop")
async def on_noop(callback: CallbackQuery) -> None:
    """Page indicator button - acknowledge and do nothing."""
    await callback.answer()


@router.callback_query(F.data == "cancel")
async def on_cancel_callback(callback: CallbackQuery, state: FSMContext) -> None:
    """Universal cancel: clear FSM state and go home."""
    await state.clear()
    await callback.answer(texts.operation_cancelled(), show_alert=False)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
@router.callback_query(FileActionFactory.filter(F.action == "open"))
async def on_file_open(
    callback: CallbackQuery,
    callback_data: FileActionFactory,
    user: User,
    storage: StorageService,
) -> None:
    """Open one file's card."""
    record = await _load_file(storage, callback_data.uid, user.telegram_id)
    if record is None:
        await _alert(callback, texts.link_not_found())
        return
    await render_file_card(callback, user=user, storage=storage, record=record)
    await callback.answer()


@router.callback_query(FileActionFactory.filter(F.action == "dl"))
async def on_file_download(
    callback: CallbackQuery,
    callback_data: FileActionFactory,
    user: User,
    storage: StorageService,
    tg_storage: TelegramStorage,
) -> None:
    """Re-send the stored file - Telegram copies it server-side (free & instant)."""
    record = await _load_file(storage, callback_data.uid, user.telegram_id)
    if record is None:
        await _alert(callback, texts.link_not_found())
        return

    await callback.answer(f"Sending {record.file_name}…")
    try:
        await tg_storage.deliver(
            callback.message.chat.id,
            record,
            caption=f"{Emoji.DOWNLOAD} <b>{record.file_name}</b>\n{format_size(record.file_size)}",
        )
    except Exception as exc:
        logger.warning("Download failed for %s: %s", record.id, exc)
        await _alert(callback, texts.error_generic("Telegram refused to send this file."))
        return

    await storage.register_view(record.id, is_download=True)


@router.callback_query(FileActionFactory.filter(F.action == "play"))
async def on_file_play(
    callback: CallbackQuery,
    callback_data: FileActionFactory,
    user: User,
    storage: StorageService,
    share: ShareService,
    settings: Settings,
) -> None:
    """Open the Mini App player for streamable media (signed short-lived ticket)."""
    record = await _load_file(storage, callback_data.uid, user.telegram_id)
    if record is None:
        await _alert(callback, texts.link_not_found())
        return
    if not record.is_streamable:
        await _alert(callback, "This file cannot be played inline - use Download.")
        return

    # Pleer Mini App ichida ochiladi: ``?play=<id>`` — ilova kirgach faylni
    # darhol ijro etadi. Avval havola alert oynasiga yozilardi: Telegram alert
    # matnini 200 belgi bilan cheklaydi (MESSAGE_TOO_LONG) va havola bosilmasdi.
    base = (settings.web.web_app_url or f"{settings.app.base_url}/index.html").split("?", 1)[0]
    if not base.startswith("https://"):
        await _alert(callback, "Pleer hozir mavjud emas — faylni yuklab oling.")
        return
    player_url = f"{base}?play={record.id}"
    await callback.answer()
    await callback.message.answer(
        f"▶️ <b>{escape_html(truncate_middle(record.file_name or 'Media', 60))}</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="▶️ Pleerda ochish", web_app=WebAppInfo(url=player_url))]]
        ),
    )


@router.callback_query(FileActionFactory.filter(F.action == "ren"))
async def on_file_rename(
    callback: CallbackQuery,
    callback_data: FileActionFactory,
    user: User,
    storage: StorageService,
    state: FSMContext,
) -> None:
    """Ask for a new name (FSM) and remember which file is being renamed."""
    record = await _load_file(storage, callback_data.uid, user.telegram_id)
    if record is None:
        await _alert(callback, texts.link_not_found())
        return

    await state.set_state(storage_handlers.RenameFileState.waiting_name)
    await state.update_data({"file_uid": callback_data.uid})
    await _edit(callback, texts.file_renamed_prompt(record))
    await callback.answer("Send the new name")


@router.callback_query(FileActionFactory.filter(F.action == "mv"))
async def on_file_move(
    callback: CallbackQuery,
    callback_data: FileActionFactory,
    user: User,
    storage: StorageService,
    state: FSMContext,
) -> None:
    """Show the destination picker."""
    record = await _load_file(storage, callback_data.uid, user.telegram_id)
    if record is None:
        await _alert(callback, texts.link_not_found())
        return

    await state.set_state(storage_handlers.MoveFileState.waiting_target)
    await state.update_data({"file_uid": callback_data.uid})
    await storage_handlers.render_move_picker(
        target=callback, user=user, storage=storage, record=record
    )


@router.callback_query(FileActionFactory.filter(F.action == "mv_to"))
async def on_file_move_to(
    callback: CallbackQuery,
    callback_data: FileActionFactory,
    user: User,
    storage: StorageService,
    state: FSMContext,
) -> None:
    """Execute the move to the picked folder (``-`` == root)."""
    await state.clear()
    record = await _load_file(storage, callback_data.uid, user.telegram_id)
    if record is None:
        await _alert(callback, texts.link_not_found())
        return

    destination: uuid.UUID | None = None if callback_data.folder == "-" else to_uuid(callback_data.folder)
    try:
        text = await storage_handlers.perform_move(
            user=user, storage=storage, record=record, folder_id=destination
        )
    except (FileNotFoundError, FolderNotFoundError, NotOwnerError) as exc:
        await _alert(callback, texts.error_generic(exc.message))
        return

    await _edit(callback, text)
    await callback.answer("Moved")


@router.callback_query(FileActionFactory.filter(F.action == "vis"))
async def on_file_visibility(
    callback: CallbackQuery,
    callback_data: FileActionFactory,
    user: User,
    storage: StorageService,
) -> None:
    """Toggle public/private and re-render the card."""
    record = await _load_file(storage, callback_data.uid, user.telegram_id)
    if record is None:
        await _alert(callback, texts.link_not_found())
        return

    updated = await storage.set_visibility(record.id, user.telegram_id, not record.is_public)
    await render_file_card(callback, user=user, storage=storage, record=updated)
    await callback.answer("Public" if updated.is_public else "Private")


@router.callback_query(FileActionFactory.filter(F.action == "share"))
async def on_file_share(
    callback: CallbackQuery,
    callback_data: FileActionFactory,
    user: User,
    storage: StorageService,
    share: ShareService,
    settings: Settings,
) -> None:
    """Open the share manager of a file."""
    record = await _load_file(storage, callback_data.uid, user.telegram_id)
    if record is None:
        await _alert(callback, texts.link_not_found())
        return
    await render_share_menu(callback=callback, user=user, record=record, share=share, settings=settings)


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "folder_new")
async def on_folder_new(callback: CallbackQuery, state: FSMContext) -> None:
    """Start the "new folder" flow."""
    await state.set_state(storage_handlers.NewFolderState.waiting_name)
    await state.update_data({"parent_folder": None})
    await _edit(callback, texts.folder_new_prompt())
    await callback.answer("Send the folder name")


@router.callback_query(ConfirmFactory.filter(F.kind == "folder"))
async def on_folder_confirm(
    callback: CallbackQuery,
    callback_data: ConfirmFactory,
    user: User,
    storage: StorageService,
    settings: Settings,
    state: FSMContext,
) -> None:
    """Two-step folder deletion (``ask`` -> ``yes_all``/``no``)."""
    folder = await _load_folder(storage, callback_data.uid, user.telegram_id)
    if folder is None:
        await _alert(callback, texts.link_not_found())
        return

    if callback_data.action == "ask":
        await storage_handlers.confirm_delete_folder(
            target=callback, user=user, storage=storage, folder=folder
        )
        return

    if callback_data.action == "no":
        await state.clear()
        await storage_handlers.render_folder(
            callback=callback, user=user, storage=storage, settings=settings, folder=folder.parent
        )
        return

    await state.clear()
    text = await storage_handlers.delete_folder_flow(
        user=user, storage=storage, folder=folder, cascade=callback_data.action == "yes_all"
    )
    await _edit(callback, text)
    await callback.answer("Deleted")


# ---------------------------------------------------------------------------
# Confirmations (files)
# ---------------------------------------------------------------------------
@router.callback_query(ConfirmFactory.filter(F.kind == "file"))
async def on_file_confirm(
    callback: CallbackQuery,
    callback_data: ConfirmFactory,
    user: User,
    storage: StorageService,
    settings: Settings,
    state: FSMContext,
) -> None:
    """Two-step file deletion."""
    record = await _load_file(storage, callback_data.uid, user.telegram_id)
    if record is None:
        await _alert(callback, texts.link_not_found())
        return

    if callback_data.action == "ask":
        await storage_handlers.confirm_delete_file(
            target=callback, user=user, storage=storage, record=record
        )
        return

    if callback_data.action == "no":
        await state.clear()
        await render_file_card(callback, user=user, storage=storage, record=record)
        return

    await state.clear()
    freed = await storage.delete_file(record.id, user.telegram_id)
    await _edit(callback, texts.file_deleted(record, freed=freed))
    await callback.answer("Deleted")


# ---------------------------------------------------------------------------
# Settings toggles
# ---------------------------------------------------------------------------
@router.callback_query(SettingsFactory.filter())
async def on_setting_toggle(
    callback: CallbackQuery,
    callback_data: SettingsFactory,
    user: User,
    storage: StorageService,
    settings: Settings,
) -> None:
    """Flip one preference and re-render the settings screen."""
    key = callback_data.key
    if key not in {"public_by_default", "inline_enabled", "notify_uploads"}:
        await _alert(callback, "Unknown setting")
        return

    current = bool(user.setting(key, key == "inline_enabled"))
    updated = await storage.update_settings(user.telegram_id, {key: not current})
    await render_settings(callback=callback, user=updated, settings=settings)
    await callback.answer(f"{key}: {'on' if not current else 'off'}")


# ---------------------------------------------------------------------------
# Share list (top-level button on the dashboard)
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "share_list")
async def on_share_list(
    callback: CallbackQuery, user: User, storage: StorageService, share: ShareService, settings: Settings
) -> None:
    """List files that currently have active share links."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from db.models import SharedLink

    rows = (
        await storage.session.scalars(
            select(File)
            .join(SharedLink, SharedLink.file_id == File.id)
            .where(File.user_id == user.telegram_id, SharedLink.is_active.is_(True))
            .options(selectinload(File.shared_links))
            .order_by(File.created_at.desc())
            .limit(15)
        )
    ).unique().all()

    if not rows:
        await _edit(
            callback,
            f"{Emoji.EMPTY} <b>No shared files yet.</b>\n"
            f"Open a file and press {Emoji.SHARE.plain()} <b>Share</b>.",
            main_menu_keyboard(user, web_app_url=settings.web.web_app_url),
        )
        await callback.answer()
        return

    lines = "".join(
        f"\n{Emoji.for_category(record.category)} {escape_html(truncate_middle(record.file_name, 34))} "
        f"— {len([link for link in record.shared_links if link.is_active_now])} link(s)"
        for record in rows
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{Emoji.SHARE.plain()} {truncate_middle(record.file_name, 30)}",
                    callback_data=FileActionFactory(action="share", uid=uid_of(record.id)).pack(),
                )
            ]
            for record in rows
        ]
        + [
            [
                InlineKeyboardButton(
                    text=f"{Emoji.HOME.plain()} Dashboard", callback_data="home"
                )
            ]
        ]
    )
    await _edit(callback, f"{Emoji.SHARE} <b>Shared files</b>{lines}", keyboard)
    await callback.answer()


# --------------------------------------------------------------------------- #
# 🚪 Darvoza (gate) tugmalari: ``auth:login`` / ``auth:register`` / ``auth:help``
# --------------------------------------------------------------------------- #
@router.callback_query(F.data == "auth:login")
async def on_gate_login(callback: CallbackQuery, user: User, state: FSMContext) -> None:
    """Darvoza: "Kirish" tugmasi → ``AuthState.waiting_login`` dialogi.

    Callback orqali reply klaviaturani o'zgartirib bo'lmaydi, shuning uchun
    dialog **chat'ga yangi xabar** bilan boshlanadi: foydalanuvchi login va
    parolni ketma-ket yuboradi (bot parolni keyin o'chiradi).
    """
    from bot.keyboards.reply import cancel_reply_keyboard
    from bot.states import AuthState

    await state.clear()
    await state.set_state(AuthState.waiting_login)
    await state.update_data(flow="login")
    await _answer_quietly(callback)
    await callback.message.answer(
        texts.login_prompt(), reply_markup=cancel_reply_keyboard(user)
    )


@router.callback_query(F.data == "auth:register")
async def on_gate_register(callback: CallbackQuery, user: User, state: FSMContext) -> None:
    """Darvoza: "Ro'yxatdan o'tish" tugmasi → ``AuthState.waiting_login`` (register oqimi)."""
    from bot.keyboards.reply import cancel_reply_keyboard
    from bot.states import AuthState

    await state.clear()
    await state.set_state(AuthState.waiting_login)
    await state.update_data(flow="register")
    await _answer_quietly(callback)
    await callback.message.answer(
        texts.register_prompt(), reply_markup=cancel_reply_keyboard(user)
    )


@router.callback_query(F.data == "auth:help")
async def on_gate_help(callback: CallbackQuery) -> None:
    """Darvoza: "Yordam" — qisqa ko'rsatma.

    Alert emas, **yangi xabar** yuboramiz: matn HTML formatda va 200 belgidan
    uzun (alert chegarasi), shuning uchun alert'ga sig'dirish uchun qisqartirish
    ma'noni yo'qotadi.
    """
    await _answer_quietly(callback)
    await callback.message.answer(texts.gate_help_short())


async def _answer_quietly(callback: CallbackQuery) -> None:
    """Callback'ni "yuklanmoqda" holatidan chiqarish (xato bo'lsa ham davom)."""
    try:
        await callback.answer()
    except Exception:
        logger.debug("callback.answer failed for %s", callback.id, exc_info=True)


def _plain_text(html_text: str) -> str:
    """HTML teglarsiz matn (alert 200 belgiga sig'ishi kerak)."""
    import re

    return re.sub(r"<[^>]+>", "", html_text).replace("&lt;", "<").replace("&gt;", ">")[:200]
