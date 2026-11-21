"""Inline keyboards and ``CallbackData`` factories.

Two rules are enforced everywhere in this module:

1. **Callback data stays under Telegram's 64-byte limit.** UUIDs are compacted
   to their 32-char ``hex`` form and prefixes are 3-7 chars long.
2. **Buttons render custom emoji** through :func:`label`, so switching to
   Telegram Premium animated icons is a configuration change, not a code change.

Factories are importable and reversible: ``FileActionFactory.filter()`` for the
handler, ``FileActionFactory(action=..., uid=...).pack()`` for the button.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.utils.emojis import Emoji, custom_emoji_enabled
from bot.utils.formatters import format_size, pages_count, truncate_middle
from db.models import File, Folder, SharedLink, User

__all__ = [
    "ConfirmFactory",
    "FileActionFactory",
    "NavFactory",
    "SettingsFactory",
    "ShareFactory",
    "cancel_button",
    "confirm_keyboard",
    "file_actions_keyboard",
    "folder_select_keyboard",
    "label",
    "main_menu_keyboard",
    "move_keyboard",
    "pagination_row",
    "settings_keyboard",
    "share_keyboard",
    "share_links_keyboard",
    "storage_keyboard",
    "to_uuid",
    "uid_of",
]


# ---------------------------------------------------------------------------
# Callback data factories
# ---------------------------------------------------------------------------
class NavFactory(CallbackData, prefix="nav"):
    """Storage browser navigation: folder + page + active search query."""

    folder: str = "-"            # "-" means root, otherwise ``uuid.hex``
    page: int = 1
    q: str = "-"                 # "-" means no search


class FileActionFactory(CallbackData, prefix="fa"):
    """Per-file actions (menu, download, rename, move, delete, visibility)."""

    action: str                  # open|dl|share|ren|mv|vis|del|back
    uid: str                     # uuid.hex (32 chars)
    folder: str = "-"            # where to return after the action


class ConfirmFactory(CallbackData, prefix="cf"):
    """Two-step destructive confirmation."""

    kind: str                    # file|folder
    action: str                  # yes|no|yes_all
    uid: str


class SettingsFactory(CallbackData, prefix="st"):
    """Preference toggles."""

    key: str                     # see SETTINGS_TOGGLES


class ShareFactory(CallbackData, prefix="sh"):
    """Share manager actions."""

    action: str                  # new|ttl|pass|revoke|public|links
    uid: str
    value: str = "-"             # ttl seconds / link token / "on"|"off"


#: Preferences exposed as toggles, with their human readable labels.
SETTINGS_TOGGLES: dict[str, str] = {
    "public_by_default": "Publish new files by default",
    "inline_enabled": "Inline search (@bot in any chat)",
    "notify_uploads": "Confirm every upload",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def uid_of(value: uuid.UUID | str) -> str:
    """Compact 32-char id safe for callback data."""
    return uuid.UUID(str(value)).hex


def to_uuid(value: str) -> uuid.UUID:
    """Inverse of :func:`uid_of`."""
    return uuid.UUID(value)


# ---------------------------------------------------------------------------
# Tugma tili va uslubi
# ---------------------------------------------------------------------------
#: Tugma yozuvlari ``APP_LANGUAGE`` ga qarab (inglizcha — kalit va zaxira).
#: Inline tugmalar handler'larga ``callback_data`` orqali boradi, shuning uchun
#: matnni tarjima qilish marshrutlashga ta'sir qilmaydi.
_LABELS: dict[str, dict[str, str]] = {
    "uz": {
        "1 hour": "1 soat", "24 hours": "24 soat", "Back": "Orqaga",
        "Back to files": "Fayllarga qaytish", "Cancel": "Bekor qilish",
        "Create new": "Yangi yaratish", "Dashboard": "Bosh sahifa", "Delete": "O'chirish",
        "Delete folder": "Papkani o'chirish", "Download": "Yuklab olish",
        "Inline mode": "Inline rejim", "Link + pass code": "Havola + kod", "Move": "Ko'chirish",
        "My files": "Fayllarim", "Never": "Muddatsiz", "New folder": "Yangi papka",
        "New link (7 days)": "Yangi havola (7 kun)", "Open Mini App": "Ilovani ochish",
        "Parent folder": "Yuqori papka", "Play in web player": "Pleerda ijro etish",
        "Refresh": "Yangilash", "Rename": "Nomini o'zgartirish", "Root (no folder)": "Asosiy (papkasiz)",
        "Search": "Qidirish", "Settings": "Sozlamalar", "Share": "Ulashish", "Sharing": "Ulashishlar",
        "Statistics": "Statistika", "Stats": "Statistika", "Web": "Ilova",
        "Delete everything": "Hammasini o'chirish", "Yes, delete": "Ha, o'chirilsin",
        "Make public": "Ommaviy qilish", "Make private": "Yopiq qilish",
    },
    "ru": {
        "1 hour": "1 час", "24 hours": "24 часа", "Back": "Назад",
        "Back to files": "К файлам", "Cancel": "Отмена", "Create new": "Создать",
        "Dashboard": "Главная", "Delete": "Удалить", "Delete folder": "Удалить папку",
        "Download": "Скачать", "Inline mode": "Инлайн-режим", "Link + pass code": "Ссылка + код",
        "Move": "Переместить", "My files": "Мои файлы", "Never": "Бессрочно",
        "New folder": "Новая папка", "New link (7 days)": "Новая ссылка (7 дней)",
        "Open Mini App": "Открыть приложение", "Parent folder": "Наверх",
        "Play in web player": "Воспроизвести", "Refresh": "Обновить", "Rename": "Переименовать",
        "Root (no folder)": "Корень (без папки)", "Search": "Поиск", "Settings": "Настройки",
        "Share": "Поделиться", "Sharing": "Ссылки", "Statistics": "Статистика", "Stats": "Статистика",
        "Web": "Приложение", "Delete everything": "Удалить всё", "Yes, delete": "Да, удалить",
        "Make public": "Сделать публичным", "Make private": "Сделать приватным",
    },
}


def tr(text: str) -> str:
    """Tugma yozuvini bot tiliga o'girish (topilmasa — asl matn)."""
    if not text:
        return text
    try:
        from config import get_settings

        lang = get_settings().app.language
    except Exception:  # pragma: no cover - sozlamasiz (testlar) holat
        return text
    return _LABELS.get(lang, {}).get(text, text)


#: Bot API 9.4+ rangli tugmalar: asosiy amal (ko'k), tasdiq (yashil), xavfli (qizil).
PRIMARY, SUCCESS, DANGER = "primary", "success", "danger"


def styled(button: InlineKeyboardButton, style: str) -> InlineKeyboardButton:
    """Tugmaga rang uslubini qo'shish (eski mijozlar e'tiborsiz qoldiradi)."""
    return button.model_copy(update={"style": style})


def label(emoji: Any, text: str, *, premium: bool = False) -> str:
    """Button caption with optional custom emoji markup.

    ``<tg-emoji>`` is rendered inside button labels too, but only for premium
    users and only when custom ids are configured - otherwise plain Unicode.
    """
    icon = emoji.render(premium=premium) if custom_emoji_enabled() else emoji.plain()
    return f"{icon} {tr(text)}"


def cancel_button(*, premium: bool = False) -> InlineKeyboardButton:
    """Universal “Cancel” button (handlers match it by text)."""
    return InlineKeyboardButton(text=label(Emoji.CLOSE, "Cancel", premium=premium), callback_data="cancel")


def pagination_row(
    page: int,
    total: int,
    *,
    page_size: int,
    factory: Any = NavFactory,
    folder: str = "-",
    query: str = "-",
    premium: bool = False,
) -> list[list[InlineKeyboardButton]]:
    """``◀️  3/8  ▶️`` row; omitted entirely when there is a single page."""
    pages = pages_count(total, page_size)
    if pages <= 1:
        return []
    row: list[InlineKeyboardButton] = []
    if page > 1:
        row.append(
            InlineKeyboardButton(
                text=label(Emoji.BACK, "", premium=premium).strip(),
                callback_data=factory(folder=folder, page=page - 1, q=query).pack(),
            )
        )
    row.append(InlineKeyboardButton(text=f"{page}/{pages}", callback_data="noop"))
    if page < pages:
        row.append(
            InlineKeyboardButton(
                text=label(Emoji.NEXT, "", premium=premium).strip(),
                callback_data=factory(folder=folder, page=page + 1, q=query).pack(),
            )
        )
    return [row]


def _markup(rows: Sequence[Sequence[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    """Build an ``InlineKeyboardMarkup`` from raw rows (filters empty rows)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[button for button in row] for row in rows if row]
    )


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------
def main_menu_keyboard(user: User, *, web_app_url: str | None = None) -> InlineKeyboardMarkup:
    """Dashboard actions shown under ``/start``."""
    premium = bool(user.is_premium)
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=label(Emoji.FOLDER_OPEN, "My files", premium=premium),
                callback_data=NavFactory().pack(),
            ),
            InlineKeyboardButton(
                text=label(Emoji.STATS, "Stats", premium=premium), callback_data="stats"
            ),
        ],
        [
            InlineKeyboardButton(
                text=label(Emoji.SEARCH, "Search", premium=premium), callback_data="search"
            ),
            InlineKeyboardButton(
                text=label(Emoji.SHARE, "Sharing", premium=premium), callback_data="share_list"
            ),
        ],
        [
            InlineKeyboardButton(
                text=label(Emoji.SETTINGS, "Settings", premium=premium), callback_data="settings"
            ),
            InlineKeyboardButton(
                text=label(Emoji.INLINE, "Inline mode", premium=premium), callback_data="inline_help"
            ),
        ],
    ]
    if web_app_url:
        rows.insert(
            0,
            [
                styled(InlineKeyboardButton(
                    text=label(Emoji.WEB_APP, "Open Mini App", premium=premium),
                    web_app={"url": web_app_url},
                ), PRIMARY)
            ],
        )
    return _markup(rows)


def storage_keyboard(
    folders: Sequence[Folder],
    files: Sequence[File],
    *,
    user: User,
    current: Folder | None = None,
    page: int = 1,
    total: int = 0,
    page_size: int = 8,
    query: str = "-",
    web_app_url: str | None = None,
) -> InlineKeyboardMarkup:
    """File browser: parent link, folders, files, then actions + pagination."""
    premium = bool(user.is_premium)
    folder_uid = uid_of(current.id) if current else "-"
    rows: list[list[InlineKeyboardButton]] = []

    if current is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    text=label(Emoji.UP, "Parent folder", premium=premium),
                    callback_data=NavFactory(
                        folder=uid_of(current.parent_id) if current.parent_id else "-", page=1, q=query
                    ).pack(),
                )
            ]
        )

    for folder in folders:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{label(Emoji.FOLDER, truncate_middle(folder.name, 32), premium=premium)}",
                    callback_data=NavFactory(folder=uid_of(folder.id), page=1, q="-").pack(),
                )
            ]
        )

    for record in files:
        icon = Emoji.for_category(record.category, premium=premium)
        caption = f"{icon} {truncate_middle(record.file_name, 34)} · {format_size(record.file_size)}"
        public = Emoji.UNLOCK.plain() if record.is_public else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{caption} {public}".rstrip(),
                    callback_data=FileActionFactory(action="open", uid=uid_of(record.id), folder=folder_uid).pack(),
                )
            ]
        )

    rows.extend(
        pagination_row(
            page,
            total,
            page_size=page_size,
            folder=folder_uid,
            query=query,
            premium=premium,
        )
    )

    action_row: list[InlineKeyboardButton] = [
        InlineKeyboardButton(
            text=label(Emoji.NEW_FOLDER, "New folder", premium=premium), callback_data="folder_new"
        ),
        InlineKeyboardButton(
            text=label(Emoji.SEARCH, "Search", premium=premium), callback_data="search"
        ),
    ]
    if current is not None:
        action_row.append(
            styled(InlineKeyboardButton(
                text=label(Emoji.DELETE, "Delete folder", premium=premium),
                callback_data=ConfirmFactory(kind="folder", action="ask", uid=folder_uid).pack(),
            ), DANGER)
        )
    rows.append(action_row)

    bottom: list[InlineKeyboardButton] = [
        InlineKeyboardButton(
            text=label(Emoji.HOME, "Dashboard", premium=premium), callback_data="home"
        ),
        InlineKeyboardButton(
            text=label(Emoji.REFRESH, "Refresh", premium=premium),
            callback_data=NavFactory(folder=folder_uid, page=page, q=query).pack(),
        ),
    ]
    if web_app_url:
        bottom.append(
            styled(InlineKeyboardButton(
                text=label(Emoji.WEB_APP, "Web", premium=premium), web_app={"url": web_app_url}
            ), PRIMARY)
        )
    rows.append(bottom)
    return _markup(rows)


def file_actions_keyboard(
    record: File, *, user: User, folder: Folder | None = None, links: Sequence[SharedLink] = ()
) -> InlineKeyboardMarkup:
    """Menu of a single file."""
    premium = bool(user.is_premium)
    uid = uid_of(record.id)
    back_folder = uid_of(folder.id) if folder else "-"
    active_links = [link for link in links if link.is_active_now]

    rows: list[list[InlineKeyboardButton]] = [
        [
            styled(InlineKeyboardButton(
                text=label(Emoji.DOWNLOAD, "Download", premium=premium),
                callback_data=FileActionFactory(action="dl", uid=uid, folder=back_folder).pack(),
            ), SUCCESS),
            InlineKeyboardButton(
                text=label(Emoji.SHARE, "Share", premium=premium),
                callback_data=ShareFactory(action="links", uid=uid).pack(),
            ),
        ],
        [
            InlineKeyboardButton(
                text=label(Emoji.RENAME, "Rename", premium=premium),
                callback_data=FileActionFactory(action="ren", uid=uid, folder=back_folder).pack(),
            ),
            InlineKeyboardButton(
                text=label(Emoji.MOVE, "Move", premium=premium),
                callback_data=FileActionFactory(action="mv", uid=uid, folder=back_folder).pack(),
            ),
        ],
        [
            InlineKeyboardButton(
                text=label(Emoji.UNLOCK if not record.is_public else Emoji.LOCK,
                           "Make public" if not record.is_public else "Make private",
                           premium=premium),
                callback_data=FileActionFactory(action="vis", uid=uid, folder=back_folder).pack(),
            ),
            styled(InlineKeyboardButton(
                text=label(Emoji.DELETE, "Delete", premium=premium),
                callback_data=ConfirmFactory(kind="file", action="ask", uid=uid).pack(),
            ), DANGER),
        ],
    ]
    if record.is_streamable:
        rows.insert(
            0,
            [
                styled(InlineKeyboardButton(
                    text=label(Emoji.PLAY, "Play in web player", premium=premium),
                    callback_data=FileActionFactory(action="play", uid=uid, folder=back_folder).pack(),
                ), PRIMARY)
            ],
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=label(Emoji.BACK, "Back to files", premium=premium),
                callback_data=NavFactory(folder=back_folder, page=1, q="-").pack(),
            )
        ]
    )
    del active_links
    return _markup(rows)


def confirm_keyboard(kind: str, uid: str, *, user: User, destructive: bool = True) -> InlineKeyboardMarkup:
    """Yes / No (and “with content” for folders) confirmation row."""
    premium = bool(user.is_premium)
    yes_text = "Delete everything" if kind == "folder" and destructive else "Yes, delete"
    rows: list[list[InlineKeyboardButton]] = [
        [
            styled(InlineKeyboardButton(
                text=label(Emoji.CHECK, yes_text, premium=premium),
                callback_data=ConfirmFactory(
                    kind=kind, action="yes_all" if kind == "folder" else "yes", uid=uid
                ).pack(),
            ), DANGER if destructive else SUCCESS),
            InlineKeyboardButton(
                text=label(Emoji.CROSS, "Cancel", premium=premium),
                callback_data=ConfirmFactory(kind=kind, action="no", uid=uid).pack(),
            ),
        ]
    ]
    return _markup(rows)


def move_keyboard(
    folders: Sequence[Folder], *, user: User, uid: str, current_folder_id: uuid.UUID | None = None
) -> InlineKeyboardMarkup:
    """Destination picker for the “Move” action."""
    premium = bool(user.is_premium)
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=label(Emoji.HOME, "Root (no folder)", premium=premium),
                callback_data=FileActionFactory(action="mv_to", uid=uid, folder="-").pack(),
            )
        ]
    ]
    for folder in folders[:20]:
        marker = " ✓" if current_folder_id and folder.id == current_folder_id else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{label(Emoji.FOLDER, truncate_middle(folder.breadcrumb(), 30), premium=premium)}{marker}",
                    callback_data=FileActionFactory(action="mv_to", uid=uid, folder=uid_of(folder.id)).pack(),
                )
            ]
        )
    rows.append([cancel_button(premium=premium)])
    return _markup(rows)


def folder_select_keyboard(
    folders: Sequence[Folder], *, user: User, action_prefix: str = "folder_pick"
) -> InlineKeyboardMarkup:
    """Generic folder picker (used by “move file into folder” flows)."""
    premium = bool(user.is_premium)
    rows = [
        [
            InlineKeyboardButton(
                text=label(Emoji.FOLDER, truncate_middle(folder.breadcrumb(), 34), premium=premium),
                callback_data=f"{action_prefix}:{uid_of(folder.id)}",
            )
        ]
        for folder in folders[:25]
    ]
    rows.append([cancel_button(premium=premium)])
    return _markup(rows or [[cancel_button(premium=premium)]])


def share_keyboard(record: File, links: Sequence[SharedLink], *, user: User) -> InlineKeyboardMarkup:
    """Create / manage links of one file."""
    premium = bool(user.is_premium)
    uid = uid_of(record.id)
    active = [link for link in links if link.is_active_now]

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=label(Emoji.LINK, "New link (7 days)", premium=premium),
                callback_data=ShareFactory(action="ttl", uid=uid, value="604800").pack(),
            )
        ],
        [
            InlineKeyboardButton(
                text=label(Emoji.CLOCK, "1 hour", premium=premium),
                callback_data=ShareFactory(action="ttl", uid=uid, value="3600").pack(),
            ),
            InlineKeyboardButton(
                text=label(Emoji.CLOCK, "24 hours", premium=premium),
                callback_data=ShareFactory(action="ttl", uid=uid, value="86400").pack(),
            ),
            InlineKeyboardButton(
                text=label(Emoji.HOURGLASS, "Never", premium=premium),
                callback_data=ShareFactory(action="ttl", uid=uid, value="0").pack(),
            ),
        ],
        [
            InlineKeyboardButton(
                text=label(Emoji.KEY, "Link + pass code", premium=premium),
                callback_data=ShareFactory(action="pass", uid=uid).pack(),
            ),
            InlineKeyboardButton(
                text=label(Emoji.UNLOCK if not record.is_public else Emoji.LOCK,
                           "Publish" if not record.is_public else "Unpublish",
                           premium=premium),
                callback_data=ShareFactory(action="public", uid=uid).pack(),
            ),
        ],
    ]

    for link in active[:5]:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{label(Emoji.CROSS, 'Revoke ' + link.token[:8], premium=premium)}",
                    callback_data=ShareFactory(action="revoke", uid=uid, value=link.token).pack(),
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text=label(Emoji.BACK, "Back", premium=premium),
                callback_data=FileActionFactory(action="open", uid=uid).pack(),
            ),
            cancel_button(premium=premium),
        ]
    )
    return _markup(rows)


def share_links_keyboard(record: File, links: Sequence[SharedLink], *, user: User) -> InlineKeyboardMarkup:
    """List of existing links with per-link actions."""
    premium = bool(user.is_premium)
    uid = uid_of(record.id)
    rows: list[list[InlineKeyboardButton]] = []
    for link in links[:8]:
        state = "active" if link.is_active_now else ("expired" if link.is_expired else "revoked")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{label(Emoji.LINK, link.token[:10], premium=premium)} · {state}",
                    callback_data=ShareFactory(action="info", uid=uid, value=link.token).pack(),
                ),
                InlineKeyboardButton(
                    text=Emoji.CROSS.plain(),
                    callback_data=ShareFactory(action="revoke", uid=uid, value=link.token).pack(),
                ),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=label(Emoji.LINK, "Create new", premium=premium),
                callback_data=ShareFactory(action="links", uid=uid).pack(),
            ),
            InlineKeyboardButton(
                text=label(Emoji.BACK, "Back", premium=premium),
                callback_data=FileActionFactory(action="open", uid=uid).pack(),
            ),
        ]
    )
    return _markup(rows)


def settings_keyboard(user: User) -> InlineKeyboardMarkup:
    """Preference toggles + utility buttons."""
    premium = bool(user.is_premium)
    values = dict(user.settings or {})
    rows: list[list[InlineKeyboardButton]] = []
    for key, text in SETTINGS_TOGGLES.items():
        enabled = bool(values.get(key, False))
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{(Emoji.CHECK if enabled else Emoji.CROSS).plain()} {text}",
                    callback_data=SettingsFactory(key=key).pack(),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=label(Emoji.STATS, "Statistics", premium=premium), callback_data="stats"
            ),
            InlineKeyboardButton(
                text=label(Emoji.HOME, "Dashboard", premium=premium), callback_data="home"
            ),
        ]
    )
    return _markup(rows)


def inline_switch_keyboard(*, query: str = "") -> InlineKeyboardMarkup:
    """Button offered with empty inline results (“search in your cloud”)."""
    return _markup(
        [
            [
                InlineKeyboardButton(
                    text=f"{Emoji.SEARCH.plain()} Browse my cloud",
                    switch_inline_query_current_chat=query,
                )
            ]
        ]
    )
