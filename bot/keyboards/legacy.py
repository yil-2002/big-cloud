"""Legacy-style keyboards: the exact menu of the original bot.

``main_menu_kb()`` reproduces the old layout one-to-one (same labels, same order,
same callback grammar) so long-time users see a familiar screen::

    🎬 Videolar   🖼️ Rasmlar
    🤖 APK/IPA    📄 Boshqalar
    📋 Barchasi   📌 Muhimlar
    📁 Papkalar   🔍 Qidirish
    📊 Statistika ➕ Papka qo'sh

The new features (Mini App, sharing, settings) are appended as extra rows, which
keeps the legacy muscle memory intact.

Callback grammar (legacy compatible, ids are the 32-char UUID hex):

===========================  =========================================
``cat:<key>:<page>``         category tab (video/photo/apps/other/all/pinned)
``folder:<uuid_hex>:<page>`` open a folder
``folders``                  folder list
``pin:<id>`` / ``unpin:<id>``📌 toggle "muhim"
``move:<id>``                choose destination folder
``domove:<id>:<folder>``     perform the move (``-`` == umumiy)
``delete:<id>``              ask for confirmation
``confirmdelete:<id>``       delete for real
``newfolder``                start the "new folder" flow
``search`` / ``stats``       search flow / statistics
``menu`` / ``noop``          back to the menu / do nothing
===========================  =========================================
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.utils.emojis import Emoji
from bot.utils.formatters import format_size, truncate_middle
from db.models import File, Folder, User

__all__ = [
    "CAT_ALL",
    "CAT_APPS",
    "CAT_KEYS",
    "CAT_OTHER",
    "CAT_PHOTO",
    "CAT_PINNED",
    "CAT_VIDEO",
    "confirm_delete_kb",
    "file_actions_kb",
    "folders_kb",
    "main_menu_kb",
    "move_folders_kb",
    "pagination_buttons",
]

#: Legacy category tab keys.
CAT_VIDEO = "video"
CAT_PHOTO = "photo"
CAT_APPS = "apps"
CAT_OTHER = "other"
CAT_ALL = "all"
CAT_PINNED = "pinned"
CAT_AUDIO = "audio"
CAT_DOCUMENT = "document"
CAT_ARCHIVE = "archive"

#: Order of the tabs in the menu (drives ``main_menu_kb``).
CAT_KEYS: tuple[str, ...] = (CAT_VIDEO, CAT_PHOTO, CAT_APPS, CAT_OTHER, CAT_ALL, CAT_PINNED)


def _id(value: uuid.UUID | str) -> str:
    """Compact id used inside callback data (32 chars, fits the 64-byte limit)."""
    return uuid.UUID(str(value)).hex


def main_menu_kb(
    user: User | None = None, *, web_app_url: str | None = None, extra_rows: bool = True
) -> InlineKeyboardMarkup:
    """The legacy main menu, optionally extended with the new features."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=f"{Emoji.VIDEO.plain()} Videolar", callback_data=f"cat:{CAT_VIDEO}:0"),
        InlineKeyboardButton(text=f"{Emoji.PHOTO.plain()} Rasmlar", callback_data=f"cat:{CAT_PHOTO}:0"),
    )
    builder.row(
        InlineKeyboardButton(text=f"{Emoji.APK.plain()} APK/IPA", callback_data=f"cat:{CAT_APPS}:0"),
        InlineKeyboardButton(text=f"{Emoji.FILE.plain()} Boshqalar", callback_data=f"cat:{CAT_OTHER}:0"),
    )
    builder.row(
        InlineKeyboardButton(text=f"{Emoji.STAR.plain()} Barchasi", callback_data=f"cat:{CAT_ALL}:0"),
        InlineKeyboardButton(text=f"{Emoji.PIN.plain()} Muhimlar", callback_data=f"cat:{CAT_PINNED}:0"),
    )
    builder.row(
        InlineKeyboardButton(text=f"{Emoji.FOLDER.plain()} Papkalar", callback_data="folders"),
        InlineKeyboardButton(text=f"{Emoji.SEARCH.plain()} Qidirish", callback_data="search"),
    )
    builder.row(
        InlineKeyboardButton(text=f"{Emoji.STATS.plain()} Statistika", callback_data="stats"),
        InlineKeyboardButton(text=f"{Emoji.NEW_FOLDER.plain()} Papka qo'sh", callback_data="newfolder"),
    )

    if extra_rows:
        builder.row(
            InlineKeyboardButton(text=f"{Emoji.SHARE.plain()} Ulashish", callback_data="share_list"),
            InlineKeyboardButton(text=f"{Emoji.SETTINGS.plain()} Sozlamalar", callback_data="settings"),
        )
        if web_app_url:
            builder.row(
                InlineKeyboardButton(
                    text=f"{Emoji.WEB_APP.plain()} Mini App", web_app=WebAppInfo(url=web_app_url)
                )
            )
    return builder.as_markup()


def file_actions_kb(file_id: uuid.UUID | str, *, pinned: bool = False) -> InlineKeyboardMarkup:
    """Per-file buttons: 📌 Pin, 📁 Ko'chirish, 🗑️ O'chirish (legacy layout)."""
    fid = _id(file_id)
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"{Emoji.PIN.plain()} Pin olish" if pinned else f"{Emoji.PIN.plain()} Pin",
            callback_data=f"unpin:{fid}" if pinned else f"pin:{fid}",
        ),
        InlineKeyboardButton(text=f"{Emoji.MOVE.plain()} Ko'chirish", callback_data=f"move:{fid}"),
        InlineKeyboardButton(text=f"{Emoji.DELETE.plain()} O'chirish", callback_data=f"delete:{fid}"),
    )
    builder.row(
        InlineKeyboardButton(text=f"{Emoji.DOWNLOAD.plain()} Yuklab olish", callback_data=f"dl:{fid}"),
        InlineKeyboardButton(text=f"{Emoji.SHARE.plain()} Ulashish", callback_data=f"fa:share:{fid}"),
        InlineKeyboardButton(text=f"{Emoji.HOME.plain()} Menyu", callback_data="menu"),
    )
    return builder.as_markup()


def pagination_buttons(context: str, page: int, total: int, *, page_size: int) -> list[InlineKeyboardButton]:
    """``⬅️  2/9  ➡️`` buttons for a legacy context string (``cat:video`` …)."""
    total_pages = max(1, (total - 1) // page_size + 1)
    buttons: list[InlineKeyboardButton] = []
    if page > 0:
        buttons.append(InlineKeyboardButton(text="⬅️", callback_data=f"{context}:{page - 1}"))
    buttons.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if (page + 1) * page_size < total:
        buttons.append(InlineKeyboardButton(text="➡️", callback_data=f"{context}:{page + 1}"))
    return buttons


def folders_kb(
    folders: Sequence[Folder], *, include_new: bool = True, include_root: bool = True
) -> InlineKeyboardMarkup:
    """Folder picker. ``umumiy`` (root) is always the first entry, as before."""
    builder = InlineKeyboardBuilder()
    if include_root:
        builder.row(
            InlineKeyboardButton(
                text=f"{Emoji.FOLDER_OPEN.plain()} umumiy", callback_data="folder:-:0"
            )
        )
    for folder in folders[:30]:
        builder.row(
            InlineKeyboardButton(
                text=f"{Emoji.FOLDER.plain()} {truncate_middle(folder.name, 30)}",
                callback_data=f"folder:{_id(folder.id)}:0",
            )
        )
    if include_new:
        builder.row(
            InlineKeyboardButton(
                text=f"{Emoji.NEW_FOLDER.plain()} Yangi papka", callback_data="newfolder"
            )
        )
    builder.row(InlineKeyboardButton(text=f"{Emoji.HOME.plain()} Menyu", callback_data="menu"))
    return builder.as_markup()


def move_folders_kb(file_id: uuid.UUID | str, folders: Sequence[Folder]) -> InlineKeyboardMarkup:
    """"Which folder should this file move to?" picker (legacy ``domove:``)."""
    fid = _id(file_id)
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"{Emoji.FOLDER_OPEN.plain()} umumiy", callback_data=f"domove:{fid}:-"
        )
    )
    for folder in folders[:30]:
        builder.row(
            InlineKeyboardButton(
                text=f"{Emoji.FOLDER.plain()} {truncate_middle(folder.name, 30)}",
                callback_data=f"domove:{fid}:{_id(folder.id)}",
            )
        )
    builder.row(InlineKeyboardButton(text="🔙 Orqaga", callback_data="menu"))
    return builder.as_markup()


def confirm_delete_kb(file_id: uuid.UUID | str) -> InlineKeyboardMarkup:
    """Two-step delete confirmation (legacy ``confirmdelete:``)."""
    fid = _id(file_id)
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Ha, o'chir", callback_data=f"confirmdelete:{fid}"),
        InlineKeyboardButton(text=f"{Emoji.CROSS.plain()} Yo'q", callback_data="menu"),
    )
    return builder.as_markup()


def file_list_row(record: File) -> InlineKeyboardButton:
    """One row of the text-mode listing (used when media sending is disabled)."""
    icon = Emoji.for_category(record.category)
    pin = f"{Emoji.PIN.plain()} " if record.is_pinned else ""
    return InlineKeyboardButton(
        text=f"{pin}{icon} {truncate_middle(record.file_name, 30)} · {format_size(record.file_size)}",
        callback_data=f"open:{_id(record.id)}",
    )
