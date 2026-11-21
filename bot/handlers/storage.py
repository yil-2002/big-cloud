"""Storage browser: folders, pagination, search, rename, move, delete.

This module owns the *message-side* of browsing (``/storage``, ``/search`` and
their FSM text flows). Button presses are routed by
:mod:`bot.handlers.callbacks`, which reuses :func:`render_folder` from here so
both entry points render exactly the same screen.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot import texts
from bot.keyboards.inline import (
    confirm_keyboard,
    move_keyboard,
    storage_keyboard,
    to_uuid,
    uid_of,
)
from bot.keyboards.reply import cancel_reply_keyboard, main_reply_keyboard
from bot.utils.emojis import Emoji
from bot.utils.formatters import pages_count
from config import Settings
from db.models import File, Folder, User
from services.exceptions import (
    FileNotFoundError,
    FolderNotEmptyError,
    FolderNotFoundError,
    NotOwnerError,
)
from services.storage import StorageService
from services.telegram import TelegramStorage

logger = logging.getLogger(__name__)

router = Router(name="storage")

#: FSM data key holding the folder currently being browsed.
DATA_FOLDER = "browse_folder"


class NewFolderState(StatesGroup):
    """Waiting for the name of a folder being created."""

    waiting_name = State()


class RenameFileState(StatesGroup):
    """Waiting for the new name of a file."""

    waiting_name = State()


class RenameFolderState(StatesGroup):
    """Waiting for the new name of a folder."""

    waiting_name = State()


class MoveFileState(StatesGroup):
    """Waiting for the destination folder of a move operation."""

    waiting_target = State()


class SearchState(StatesGroup):
    """Waiting for a search query."""

    waiting_query = State()


# ---------------------------------------------------------------------------
# Rendering (shared with the callback router)
# ---------------------------------------------------------------------------
async def render_folder(
    *,
    message: Message | None = None,
    callback: CallbackQuery | None = None,
    user: User,
    storage: StorageService,
    settings: Settings,
    folder: Folder | None = None,
    page: int = 1,
    query: str | None = None,
    edit: bool = True,
) -> None:
    """Render the storage browser as a new message or an edit of the current one.

    Exactly one of ``message`` / ``callback`` must be provided. Results are
    paginated with ``PAGE_SIZE``; folders are always shown in full (they are few
    and they are the navigation itself).
    """
    page_size = settings.storage.page_size
    folder_id = folder.id if folder else None

    folders = await storage.list_folders(user.telegram_id, parent_id=folder_id)
    files, total = await storage.list_files(
        user.telegram_id,
        folder_id=folder_id,
        page=page,
        page_size=page_size,
        search=query,
    )
    page = max(1, min(page, pages_count(total, page_size)))

    text = texts.folder_list(
        folders,
        files,
        current=folder,
        page=page,
        pages=pages_count(total, page_size),
        total_files=total,
        query=query,
    )
    keyboard = storage_keyboard(
        folders,
        files,
        user=user,
        current=folder,
        page=page,
        total=total,
        page_size=page_size,
        query=query or "-",
        web_app_url=settings.web.web_app_url,
    )

    if callback is not None and edit:
        try:
            await callback.message.edit_text(text, reply_markup=keyboard)
        except Exception as exc:
            logger.debug("Edit skipped while rendering folder: %s", exc)
        await callback.answer()
        return

    target = message or (callback.message if callback else None)
    if target is None:  # pragma: no cover - defensive
        return
    await target.answer(text, reply_markup=keyboard)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
async def open_root(
    message: Message,
    *,
    user: User,
    storage: StorageService,
    settings: Settings,
    state: FSMContext,
) -> None:
    """Open the browser at the root folder (``/storage`` and its reply button)."""
    await state.clear()
    await state.update_data({DATA_FOLDER: None})
    await render_folder(
        message=message, user=user, storage=storage, settings=settings, folder=None, page=1
    )


async def start_search(message: Message, *, state: FSMContext) -> None:
    """Ask the user for a search query (``/search`` and its reply button)."""
    await state.set_state(SearchState.waiting_query)
    await message.answer(texts.search_prompt(), reply_markup=cancel_reply_keyboard())


@router.message(SearchState.waiting_query, F.text)
async def on_search_query(
    message: Message,
    user: User,
    storage: StorageService,
    settings: Settings,
    tg_storage: TelegramStorage,
    state: FSMContext,
) -> None:
    """Handle the typed search query.

    Results are rendered by the legacy browser (real media + 📌/📁/🗑️ buttons);
    set ``BROWSE_SEND_MEDIA=false`` for the compact button list instead.
    """
    query = (message.text or "").strip()
    if not query or query.lower().startswith("/"):
        await state.clear()
        await message.answer(
            texts.operation_cancelled(),
            reply_markup=main_reply_keyboard(user, web_app_url=settings.web.web_app_url),
        )
        return

    await state.clear()
    from bot.handlers.browsing import send_search_results

    await send_search_results(
        message,
        query=query,
        user=user,
        storage=storage,
        settings=settings,
        tg_storage=tg_storage,
    )


# ---------------------------------------------------------------------------
# Folder creation
# ---------------------------------------------------------------------------
@router.message(NewFolderState.waiting_name, F.text)
async def on_new_folder_name(
    message: Message,
    user: User,
    storage: StorageService,
    settings: Settings,
    state: FSMContext,
) -> None:
    """Create the folder whose name the user just typed."""
    name = (message.text or "").strip()
    data = await state.get_data()
    parent_raw = data.get("parent_folder")
    await state.clear()

    if not name:
        await message.answer(texts.folder_new_prompt())
        return

    parent_id = to_uuid(parent_raw) if parent_raw else None
    try:
        folder = await storage.create_folder(name, user.telegram_id, parent_id)
    except FolderNotFoundError as exc:
        await message.answer(texts.error_generic(exc.message))
        return

    await message.answer(
        texts.folder_created(folder),
        reply_markup=main_reply_keyboard(user, web_app_url=settings.web.web_app_url),
    )
    await render_folder(
        message=message,
        user=user,
        storage=storage,
        settings=settings,
        folder=folder.parent if folder.parent_id else None,
        page=1,
    )


# ---------------------------------------------------------------------------
# Rename
# ---------------------------------------------------------------------------
@router.message(RenameFileState.waiting_name, F.text)
async def on_rename_file_name(
    message: Message,
    user: User,
    storage: StorageService,
    settings: Settings,
    state: FSMContext,
) -> None:
    """Apply a new file name typed by the user."""
    new_name = (message.text or "").strip()
    data = await state.get_data()
    file_raw = data.get("file_uid")
    await state.clear()

    if not file_raw:
        await message.answer(texts.error_generic())
        return
    if not new_name:
        await message.answer(texts.operation_cancelled())
        return

    try:
        record = await storage.get_file(to_uuid(file_raw), user.telegram_id)
        old_name = record.file_name
        record = await storage.rename_file(record.id, user.telegram_id, new_name)
    except (FileNotFoundError, NotOwnerError) as exc:
        await message.answer(texts.error_generic(exc.message))
        return

    await message.answer(texts.file_renamed(old_name, record.file_name))
    await render_folder(
        message=message,
        user=user,
        storage=storage,
        settings=settings,
        folder=record.folder,
        page=1,
    )


@router.message(RenameFolderState.waiting_name, F.text)
async def on_rename_folder_name(
    message: Message,
    user: User,
    storage: StorageService,
    settings: Settings,
    state: FSMContext,
) -> None:
    """Apply a new folder name typed by the user."""
    new_name = (message.text or "").strip()
    data = await state.get_data()
    folder_raw = data.get("folder_uid")
    await state.clear()

    if not folder_raw or not new_name:
        await message.answer(texts.operation_cancelled())
        return

    try:
        folder = await storage.rename_folder(to_uuid(folder_raw), user.telegram_id, new_name)
    except FolderNotFoundError as exc:
        await message.answer(texts.error_generic(exc.message))
        return

    await message.answer(texts.folder_created(folder))
    await render_folder(
        message=message,
        user=user,
        storage=storage,
        settings=settings,
        folder=folder.parent if folder.parent_id else None,
        page=1,
    )


# ---------------------------------------------------------------------------
# Move
# ---------------------------------------------------------------------------
@router.message(MoveFileState.waiting_target)
async def on_move_target_message(message: Message, user: User) -> None:
    """A destination must be picked with a button, not typed."""
    await message.answer(
        f"{Emoji.MOVE} Pick a destination from the buttons below "
        f"(or press {Emoji.CLOSE.plain()} Cancel)."
    )


async def render_move_picker(
    *,
    target: Message | CallbackQuery,
    user: User,
    storage: StorageService,
    record: File,
) -> None:
    """Show the destination picker for a "Move" action."""
    folders = await _all_folders(storage, user.telegram_id)
    keyboard = move_keyboard(folders, user=user, uid=uid_of(record.id), current_folder_id=record.folder_id)
    text = texts.move_target_prompt(record, folder_count=len(folders))
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=keyboard)
        await target.answer()
    else:
        await target.answer(text, reply_markup=keyboard)


async def perform_move(
    *,
    user: User,
    storage: StorageService,
    record: File,
    folder_id: uuid.UUID | None,
) -> str:
    """Execute a move and return the confirmation text."""
    moved = await storage.move_file(record.id, user.telegram_id, folder_id)
    folder = await storage.get_folder(folder_id, user.telegram_id) if folder_id else None
    return texts.file_moved(moved, folder)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------
async def confirm_delete_file(
    *, target: Message | CallbackQuery, user: User, storage: StorageService, record: File
) -> None:
    """Ask for confirmation before deleting a file."""
    text = texts.confirm_delete_file(record)
    keyboard = confirm_keyboard("file", uid_of(record.id), user=user)
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=keyboard)
        await target.answer()
    else:
        await target.answer(text, reply_markup=keyboard)


async def confirm_delete_folder(
    *, target: Message | CallbackQuery, user: User, storage: StorageService, folder: Folder
) -> None:
    """Ask for confirmation before deleting a folder."""
    files, folders = await _folder_content_counts(storage, folder.id)
    text = texts.confirm_delete_folder(folder, files=files, subfolders=folders)
    keyboard = confirm_keyboard("folder", uid_of(folder.id), user=user, destructive=True)
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=keyboard)
        await target.answer()
    else:
        await target.answer(text, reply_markup=keyboard)


async def delete_folder_flow(
    *,
    user: User,
    storage: StorageService,
    folder: Folder,
    cascade: bool,
) -> str:
    """Delete a folder, returning the user-facing result text."""
    try:
        removed = await storage.delete_folder(folder.id, user.telegram_id, cascade=cascade)
    except FolderNotEmptyError as exc:
        return texts.folder_not_empty(
            files=int(exc.details.get("files", 0)), folders=int(exc.details.get("folders", 0))
        )
    except FolderNotFoundError as exc:
        return texts.error_generic(exc.message)
    return texts.folder_deleted(folder.name, files_removed=removed)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
async def _all_folders(storage: StorageService, telegram_id: int) -> list[Folder]:
    """Every folder of a user, parents first (used by the move picker)."""
    from sqlalchemy import select

    rows = (
        await storage.session.scalars(
            select(Folder).where(Folder.user_id == telegram_id).order_by(Folder.created_at)
        )
    ).unique().all()
    return list(rows)


async def _folder_content_counts(
    storage: StorageService, folder_id: uuid.UUID
) -> tuple[int, int]:
    """Return ``(file_count, subfolder_count)`` for a folder."""
    from sqlalchemy import func, select

    files = int(
        await storage.session.scalar(
            select(func.count()).select_from(File).where(File.folder_id == folder_id)
        )
        or 0
    )
    folders = int(
        await storage.session.scalar(
            select(func.count()).select_from(Folder).where(Folder.parent_id == folder_id)
        )
        or 0
    )
    return files, folders


def folder_uid_or_none(data: dict[str, Any]) -> uuid.UUID | None:
    """Read the browsed folder id from FSM data (``None`` == root)."""
    raw = data.get(DATA_FOLDER)
    return to_uuid(raw) if raw else None
