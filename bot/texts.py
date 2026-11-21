"""Every user-facing string of the bot lives here.

Keeping copy in one module (instead of scattered f-strings) means:

* the UI can be re-worded or translated without touching handler logic;
* messages are unit-testable (they are plain functions);
* HTML escaping is applied consistently through :mod:`bot.utils.formatters`.

All functions return ``parse_mode=HTML`` markup and use the :class:`Emoji`
registry, so custom emoji support is inherited automatically.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from bot.utils.emojis import Emoji
from bot.utils.formatters import (
    escape_html,
    format_date,
    format_duration,
    format_number,
    format_relative_time,
    format_size,
    human_size_pair,
    key_value_line,
    mask_token,
    pluralize,
    progress_bar,
    storage_status_emoji,
    truncate_middle,
)
from db.models import File, Folder, SharedLink, User

__all__ = [
    "CATEGORY_TITLES",
    "COMMANDS",
    "album_saved",
    "auth_required",
    "category_header",
    "category_title",
    "confirm_delete_file",
    "confirm_delete_folder",
    "dashboard",
    "deep_link_file_saved",
    "deep_link_not_found",
    "empty_folder",
    "error_generic",
    "file_caption",
    "file_card",
    "file_deleted",
    "file_moved",
    "file_renamed_prompt",
    "file_saved",
    "folder_created",
    "folder_deleted",
    "folder_list",
    "folder_new_prompt",
    "help_text",
    "inline_disabled",
    "inline_empty",
    "inline_hint",
    "link_created",
    "link_list",
    "link_revoked",
    "move_target_prompt",
    "no_files_yet",
    "not_owner",
    "pass_code_wrong",
    "password_locked",
    "password_ok",
    "password_prompt",
    "password_wrong",
    "pin_toggled",
    "quota_exceeded",
    "search_empty",
    "search_prompt",
    "search_results",
    "setting_toggled",
    "settings_text",
    "share_menu",
    "too_large",
    "unsupported_media",
    "welcome",
]

#: Command list - paste the same text into BotFather ``/setcommands``.
COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "Open the cloud dashboard"),
    ("storage", "Browse my files and folders"),
    ("upload", "How to upload a file"),
    ("search", "Search by file name"),
    ("share", "Manage public links"),
    ("inline", "How to use inline mode"),
    ("stats", "Storage statistics"),
    ("settings", "Preferences"),
    ("webapp", "Open the Mini App"),
    ("help", "Show this help"),
)


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------
def welcome(user: User, *, bot_username: str, web_app_url: str | None = None) -> str:
    """First-contact message with the product pitch and the main keyboard hint."""
    lines = [
        f"{Emoji.CLOUD} <b>Welcome to your personal cloud, {escape_html(user.first_name or 'friend')}!</b>",
        "",
        "Send me <b>any file</b> — a document, photo, video, audio or voice note —",
        "and I will keep it in your private Telegram cloud. Nothing is stored on",
        "a third-party server: Telegram itself is the storage.",
        "",
        f"{Emoji.UPLOAD} <b>Upload</b> — just send a file to this chat",
        f"{Emoji.DOWNLOAD} <b>Download</b> — I re-send it instantly, no bandwidth limits",
        f"{Emoji.SHARE} <b>Share</b> — public links with expiry and a pass code",
        f"{Emoji.INLINE} <b>Inline</b> — type <code>@{escape_html(bot_username)} name</code> in any chat",
        f"{Emoji.WEB_APP} <b>Mini App</b> — full file manager in one tap",
        "",
        f"Your quota: <b>{format_size(user.free_space(_quota_of(user)))}</b> free.",
    ]
    if web_app_url:
        lines.append(f"\n{Emoji.SPARKLES} Open the web interface: {escape_html(web_app_url)}")
    return "\n".join(lines)


def help_text(*, bot_username: str) -> str:
    """``/help`` output: commands + short usage notes."""
    command_lines = "\n".join(
        f"{Emoji.STAR} <b>/{name}</b> — {escape_html(description)}" for name, description in COMMANDS
    )
    return "\n".join(
        [
            f"{Emoji.ROBOT} <b>Telegram Cloud Drive — help</b>",
            "",
            command_lines,
            "",
            f"{Emoji.INFO} <b>Tips</b>",
            "• Send several files at once (album) — I store each of them.",
            "• Caption a file and the caption is saved with it.",
            f"• In any chat type <code>@{escape_html(bot_username)}</code> to search your cloud.",
            "• Deep link <code>t.me/" + escape_html(bot_username) + "?start=&lt;token&gt;</code> opens a shared file.",
        ]
    )


def dashboard(
    user: User,
    stats: dict[str, Any],
    *,
    bot_username: str,
    is_admin: bool = False,
) -> str:
    """Main screen: quota bar + counters + quick actions."""
    quota = int(stats.get("quota") or 0)
    used = int(stats.get("used") or 0)
    indicator = storage_status_emoji(used, quota)

    header = (
        f"{Emoji.CLOUD} <b>Your cloud storage</b>"
        + (f" {Emoji.STAR}<i>admin</i>" if is_admin else "")
    )
    category_lines = "".join(
        f"\n  {Emoji.for_category(name)} {escape_html(name.title())}: "
        f"<b>{format_number(item['count'])}</b> ({format_size(item['size'])})"
        for name, item in sorted(
            (stats.get("by_category") or {}).items(), key=lambda kv: -kv[1]["size"]
        )
    )

    return "\n".join(
        [
            header,
            "",
            f"{indicator} <code>{progress_bar(used, quota or 1, width=12)}</code>",
            f"{Emoji.STATS} {key_value_line('Used', human_size_pair(used, quota))}",
            f"{Emoji.FILE} {key_value_line('Files', pluralize(int(stats.get('files') or 0), 'file'))}",
            f"{Emoji.FOLDER} {key_value_line('Folders', pluralize(int(stats.get('folders') or 0), 'folder'))}",
            f"{Emoji.LINK} {key_value_line('Public', pluralize(int(stats.get('public_files') or 0), 'file'))}",
            category_lines or "",
            "",
            f"{Emoji.UPLOAD} Send me a file to upload it.",
            f"{Emoji.INLINE} Search from any chat: <code>@{escape_html(bot_username)} &lt;name&gt;</code>",
        ]
    )


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------
def file_saved(record: File, *, user: User, quota: int, position: int = 1, total: int = 1) -> str:
    """Confirmation card shown right after a successful upload."""
    counter = f" ({position}/{total})" if total > 1 else ""
    return (
        f"✅ <b>{escape_html(record.file_name)}</b>{counter} yuklandi\n"
        f"💾 {format_size(record.file_size)}"
    )


def album_saved(records: Sequence[File], *, user: User, quota: int) -> str:
    """One summary card for a media-group upload."""
    names = "\n".join(
        f"{Emoji.for_category(record.category)} {escape_html(truncate_middle(record.file_name, 40))} ✅"
        for record in records[:10]
    )
    more = f"\n… va yana {len(records) - 10} ta ✅" if len(records) > 10 else ""
    return f"{names}{more}\n\n{len(records)} ta fayl yuklandi ✅"


def too_large(size: int, limit: int) -> str:
    """Reject an oversized upload with the exact numbers."""
    return "\n".join(
        [
            f"{Emoji.ERROR} <b>File is too large</b>",
            "",
            f"{key_value_line('Received', format_size(size))}",
            f"{key_value_line('Limit', format_size(limit))}",
            "",
            f"{Emoji.INFO} Use a kengaytirilgan rejim to raise this limit.",
        ]
    )


def quota_exceeded(*, required: int, free: int, quota: int, used: int) -> str:
    """Explain a quota rejection and offer the obvious way out."""
    return "\n".join(
        [
            f"{Emoji.STORAGE_FULL} <b>Not enough free space</b>",
            "",
            f"{key_value_line('File needs', format_size(required))}",
            f"{key_value_line('Free now', format_size(free))}",
            f"{key_value_line('Quota', human_size_pair(used, quota))}",
            "",
            f"{Emoji.DELETE} Delete something with <b>/storage</b>, then send the file again.",
        ]
    )


def duplicate_file(name: str) -> str:
    """De-duplication notice (same ``file_unique_id`` already stored)."""
    return (
        f"{Emoji.INFO} <b>Already in your cloud</b>\n"
        f"{Emoji.FILE} {escape_html(truncate_middle(name, 48))}\n\n"
        f"Identical files are stored only once — open it via {Emoji.FOLDER} <b>/storage</b>."
    )


def unsupported_media() -> str:
    """Sent when the message carries something we cannot store."""
    return (
        f"{Emoji.WARNING} <b>I cannot store this yet.</b>\n"
        "Supported: documents, photos, videos, audio, voice notes, video notes, "
        "animations and stickers.\n\n"
        f"Type {Emoji.SEARCH} <b>/help</b> for the full feature list."
    )


def upload_hint() -> str:
    """``/upload`` explanation."""
    return "\n".join(
        [
            f"{Emoji.UPLOAD} <b>How to upload</b>",
            "",
            "1. Send me the file in this chat (as a document, photo, video or audio).",
            "2. Add a caption if you want — it is stored with the file.",
            "3. You get a card with download / share / delete buttons.",
            "",
            f"{Emoji.SPARKLES} Albums are supported: send up to 10 files at once.",
            f"{Emoji.WARNING} Single-file limit and quota are shown by <b>/stats</b>.",
        ]
    )


# ---------------------------------------------------------------------------
# Browsing
# ---------------------------------------------------------------------------
def file_card(record: File, *, folder: Folder | None = None, links: Sequence[SharedLink] = ()) -> str:
    """Detailed file view opened from the storage browser."""
    active = [link for link in links if link.is_active_now]
    link_line = ""
    if active:
        link = active[0]
        expires = (
            f"expires {format_relative_time(link.expires_at).replace(' ago', '')}"
            if link.expires_at
            else "never expires"
        )
        lock = f" {Emoji.KEY}" if link.has_pass_code else ""
        link_line = f"\n{Emoji.LINK} {key_value_line('Link', mask_token(link.token) + lock)} <i>({expires})</i>"

    extra: list[str] = []
    if record.duration:
        extra.append(key_value_line("Duration", format_duration(record.duration)))
    if record.width and record.height:
        extra.append(key_value_line("Resolution", f"{record.width}×{record.height}"))
    if record.mime_type:
        extra.append(key_value_line("Type", record.mime_type))

    return "\n".join(
        [
            f"{Emoji.for_category(record.category)} <b>{escape_html(truncate_middle(record.file_name, 60))}</b>",
            "",
            f"{Emoji.STATS} {key_value_line('Size', format_size(record.file_size))}",
            f"{Emoji.CLOCK} {key_value_line('Added', format_date(record.created_at))}",
            f"{Emoji.FOLDER} {key_value_line('Folder', folder.breadcrumb() if folder else 'Root')}",
            *extra,
            f"{Emoji.EYE} {key_value_line('Views', format_number(record.views))}"
            f"  ·  {Emoji.DOWNLOAD.plain()} {format_number(record.downloads)}",
            f"{Emoji.LOCK if not record.is_public else Emoji.UNLOCK} "
            f"{key_value_line('Visibility', 'Public' if record.is_public else 'Private')}",
            link_line,
        ]
    )


def folder_list(
    folders: Sequence[Folder],
    files: Sequence[File],
    *,
    current: Folder | None,
    page: int,
    pages: int,
    total_files: int,
    query: str | None = None,
) -> str:
    """Storage browser screen (folders are rendered as keyboard buttons)."""
    location = current.breadcrumb() if current else "Root"
    title = f"{Emoji.SEARCH} Search: “{escape_html(query)}”" if query else f"{Emoji.FOLDER_OPEN} {escape_html(location)}"
    file_lines = "".join(
        f"\n{Emoji.for_category(record.category)} {escape_html(truncate_middle(record.file_name, 40))} "
        f"<i>({format_size(record.file_size)})</i>"
        for record in files
    )
    footer = (
        f"\n\n{Emoji.INFO} Page <b>{page}/{pages}</b> · "
        f"{pluralize(total_files, 'file')}"
        f" · {pluralize(len(folders), 'folder')}"
    )
    if not files and not folders:
        return (
            f"{title}\n\n{Emoji.EMPTY} <b>Nothing here yet.</b>\n"
            f"{Emoji.UPLOAD} Send me a file, or create a folder with the button below."
        )
    hint = "\n\nTap a file to open its menu." if files else ""
    return f"{title}{file_lines}{footer}{hint}"


def empty_folder() -> str:
    """Shown for a folder without content."""
    return (
        f"{Emoji.EMPTY} <b>This folder is empty.</b>\n"
        f"{Emoji.UPLOAD} Send a file and choose “Move” to place it here."
    )


def folder_new_prompt() -> str:
    """FSM prompt: waiting for a folder name."""
    return (
        f"{Emoji.NEW_FOLDER} <b>New folder</b>\n"
        "Send me the folder name (up to 255 characters), or press "
        f"{Emoji.CLOSE} <b>Cancel</b>."
    )


def folder_created(folder: Folder) -> str:
    return f"{Emoji.CHECK} Folder <b>{escape_html(folder.name)}</b> created."


def folder_deleted(name: str, *, files_removed: int) -> str:
    removed = f" ({pluralize(files_removed, 'file')} deleted)" if files_removed else ""
    return f"{Emoji.DELETE} Folder <b>{escape_html(name)}</b> deleted{removed}."


def folder_not_empty(*, files: int, folders: int) -> str:
    return (
        f"{Emoji.WARNING} <b>Folder is not empty</b>\n"
        f"{pluralize(files, 'file')} and {pluralize(folders, 'sub-folder')} inside.\n"
        f"Use “Delete with content” to remove everything at once."
    )


def file_renamed_prompt(record: File) -> str:
    """FSM prompt: waiting for a new file name."""
    return (
        f"{Emoji.RENAME} <b>Rename file</b>\n"
        f"Current name: <code>{escape_html(record.file_name)}</code>\n"
        f"Send the new name, or press {Emoji.CLOSE} <b>Cancel</b>."
    )


def file_renamed(old: str, new: str) -> str:
    return (
        f"{Emoji.CHECK} Renamed:\n"
        f"<code>{escape_html(truncate_middle(old, 40))}</code> → "
        f"<code>{escape_html(truncate_middle(new, 40))}</code>"
    )


def move_target_prompt(record: File, *, folder_count: int) -> str:
    """FSM/keyboard prompt for choosing a destination folder."""
    return (
        f"{Emoji.MOVE} <b>Move “{escape_html(truncate_middle(record.file_name, 32))}”</b>\n"
        f"Choose a destination ({pluralize(folder_count, 'folder')} available) "
        f"or send it back to {Emoji.HOME} <b>Root</b>."
    )


def file_moved(record: File, folder: Folder | None) -> str:
    destination = folder.breadcrumb() if folder else "Root"
    return (
        f"{Emoji.CHECK} <b>{escape_html(truncate_middle(record.file_name, 40))}</b> "
        f"moved to {Emoji.FOLDER} <b>{escape_html(destination)}</b>."
    )


def confirm_delete_file(record: File) -> str:
    """Destructive action confirmation (two-step)."""
    return (
        f"{Emoji.WARNING} <b>Delete this file?</b>\n\n"
        f"{Emoji.for_category(record.category)} {escape_html(truncate_middle(record.file_name, 48))}\n"
        f"{Emoji.STATS} {format_size(record.file_size)} will be freed.\n\n"
        "This also revokes all of its share links. The action cannot be undone."
    )


def confirm_delete_folder(folder: Folder, *, files: int, subfolders: int) -> str:
    return (
        f"{Emoji.WARNING} <b>Delete the folder “{escape_html(folder.name)}”?</b>\n\n"
        f"Inside: {pluralize(files, 'file')} and {pluralize(subfolders, 'sub-folder')}.\n"
        "Everything will be removed permanently."
    )


def file_deleted(record: File, *, freed: int) -> str:
    return (
        f"{Emoji.DELETE} <b>Deleted</b> {escape_html(truncate_middle(record.file_name, 48))}\n"
        f"{Emoji.STORAGE_OK} Freed {format_size(freed)}."
    )


def search_prompt() -> str:
    return (
        f"{Emoji.SEARCH} <b>Search</b>\n"
        "Send me a file name (or part of it) and I will show the matches.\n"
        f"{Emoji.CLOSE} Press Cancel to abort."
    )


def search_results(records: Sequence[File], query: str) -> str:
    if not records:
        return (
            f"{Emoji.SEARCH} No results for “{escape_html(query)}”.\n"
            f"{Emoji.INFO} Try a shorter query, or browse everything with <b>/storage</b>."
        )
    lines = "".join(
        f"\n{Emoji.for_category(record.category)} {escape_html(truncate_middle(record.file_name, 42))} "
        f"<i>({format_size(record.file_size)})</i>"
        for record in records[:15]
    )
    return f"{Emoji.SEARCH} <b>{pluralize(len(records), 'match')}</b> for “{escape_html(query)}”:{lines}"


# ---------------------------------------------------------------------------
# Sharing
# ---------------------------------------------------------------------------
def share_menu(record: File, links: Sequence[SharedLink]) -> str:
    """Entry screen of the share manager for one file."""
    active = [link for link in links if link.is_active_now]
    if not active:
        state = f"{Emoji.LOCK} <b>Private</b> — no active links."
    else:
        state = f"{Emoji.UNLOCK} <b>{pluralize(len(active), 'active link')}</b>."
    return "\n".join(
        [
            f"{Emoji.SHARE} <b>Sharing</b>",
            f"{Emoji.for_category(record.category)} {escape_html(truncate_middle(record.file_name, 48))}",
            "",
            state,
            "",
            "Create a link with an expiry, protect it with a pass code,",
            "or publish the file so it appears in search.",
        ]
    )


def link_created(
    link: SharedLink,
    record: File,
    *,
    deep_link: str,
    web_url: str,
    pass_code: str | None = None,
) -> str:
    """Freshly minted share link with both entry points."""
    expires = (
        f"expires in {format_relative_time(link.expires_at).replace(' ago', '')}"
        if link.expires_at
        else "never expires"
    )
    lines = [
        f"{Emoji.LINK} <b>Link created</b>",
        f"{Emoji.for_category(record.category)} {escape_html(truncate_middle(record.file_name, 48))}",
        "",
        f"{Emoji.ROBOT} <b>Telegram:</b>\n<code>{escape_html(deep_link)}</code>",
        f"{Emoji.WEB_APP} <b>Web:</b>\n<code>{escape_html(web_url)}</code>",
        "",
        f"{Emoji.CLOCK} {escape_html(expires)}",
    ]
    if pass_code:
        lines.append(f"{Emoji.KEY} <b>Pass code:</b> <code>{escape_html(pass_code)}</code>")
    lines.append(f"\n{Emoji.INFO} Share either link — the pass code is asked once.")
    return "\n".join(lines)


def link_list(record: File, links: Sequence[SharedLink]) -> str:
    """All links of a file with their state (buttons do the actions)."""
    if not links:
        return f"{Emoji.EMPTY} No links yet for <b>{escape_html(truncate_middle(record.file_name, 40))}</b>."
    rows: list[str] = []
    for link in links[:10]:
        if not link.is_active:
            state = f"{Emoji.CROSS} revoked"
        elif link.is_expired:
            state = f"{Emoji.CLOCK} expired"
        else:
            state = f"{Emoji.CHECK} active"
        lock = f" {Emoji.KEY}" if link.has_pass_code else ""
        stats = f"{Emoji.EYE.plain()} {link.views} · {Emoji.DOWNLOAD.plain()} {link.downloads}"
        rows.append(
            f"\n<code>{escape_html(mask_token(link.token, visible=6))}</code>{lock} — {state}"
            f"\n   {stats} · created {format_relative_time(link.created_at)}"
        )
    return (
        f"{Emoji.SHARE} <b>Links of {escape_html(truncate_middle(record.file_name, 36))}</b>"
        + "".join(rows)
    )


def link_revoked(token: str) -> str:
    return f"{Emoji.CROSS} Link <code>{escape_html(mask_token(token))}</code> revoked."


def link_expired_message() -> str:
    return f"{Emoji.CLOCK} <b>This link has expired.</b>\nAsk the owner for a new one."


def link_revoked_message() -> str:
    return f"{Emoji.CROSS} <b>This link was revoked by its owner.</b>"


def link_not_found() -> str:
    return f"{Emoji.ERROR} <b>Link not found.</b>\nIt may have been deleted."


def pass_code_prompt() -> str:
    return (
        f"{Emoji.KEY} <b>Access code required</b>\n"
        "Send the numeric code you received from the file owner."
    )


# ---------------------------------------------------------------------------
# Deep links
# ---------------------------------------------------------------------------
def deep_link_file_saved(record: File, *, owner_name: str | None = None) -> str:
    """A shared file was saved into the recipient's own cloud."""
    source = f" from {escape_html(owner_name)}" if owner_name else ""
    return "\n".join(
        [
            f"{Emoji.CHECK} <b>Saved to your cloud{source}</b>",
            "",
            f"{Emoji.for_category(record.category)} {escape_html(truncate_middle(record.file_name, 48))}",
            f"{Emoji.STATS} {key_value_line('Size', format_size(record.file_size))}",
            "",
            f"{Emoji.DOWNLOAD} The file is above — tap to play or download.",
            f"{Emoji.FOLDER} Manage it any time with <b>/storage</b>.",
        ]
    )


def deep_link_not_found(payload: str) -> str:
    return (
        f"{Emoji.ERROR} <b>Nothing found for this link.</b>\n"
        f"Payload: <code>{escape_html(mask_token(payload, visible=8))}</code>\n\n"
        "The file may have been deleted or the link expired."
    )


# ---------------------------------------------------------------------------
# Inline mode
# ---------------------------------------------------------------------------
def inline_hint(*, bot_username: str) -> str:
    """``/inline`` explanation."""
    return "\n".join(
        [
            f"{Emoji.INLINE} <b>Inline mode</b>",
            "",
            f"Type <code>@{escape_html(bot_username)} &lt;file name&gt;</code> in <b>any</b> chat —",
            "a group, a channel comment or another person's DM — and pick one of",
            "your stored files. It is sent instantly, without re-uploading.",
            "",
            f"{Emoji.LOCK} Results are shown <b>only to you</b>; the recipient cannot",
            "browse your cloud.",
            f"{Emoji.SETTINGS} Turn it off any time in <b>/settings</b>.",
        ]
    )


def inline_empty(query: str) -> str:
    return f"{Emoji.EMPTY} Nothing matches “{escape_html(query)}” in your cloud."


def inline_disabled() -> str:
    return (
        f"{Emoji.LOCK} <b>Inline search is disabled</b> in your settings.\n"
        f"Enable it with {Emoji.SETTINGS} <b>/settings</b>."
    )


# ---------------------------------------------------------------------------
# Settings & stats
# ---------------------------------------------------------------------------
def settings_text(user: User, settings_map: dict[str, Any]) -> str:
    """Preferences screen; buttons carry the toggles."""
    rows = "".join(
        f"\n{_toggle_icon(value)} {escape_html(label)}"
        for label, value in settings_map.items()
    )
    return (
        f"{Emoji.SETTINGS} <b>Settings</b>{rows}\n\n"
        f"{Emoji.INFO} Tap a line to flip it. Changes apply immediately."
    )


def setting_toggled(label: str, value: bool) -> str:
    icon = Emoji.CHECK if value else Emoji.CROSS
    return f"{icon} <b>{escape_html(label)}</b>: {'on' if value else 'off'}"


def stats_text(user: User, stats: dict[str, Any], *, max_file_size: int) -> str:
    """``/stats`` screen: quota, counters, per-category breakdown."""
    quota = int(stats.get("quota") or 0)
    used = int(stats.get("used") or 0)
    rows = "".join(
        f"\n  {Emoji.for_category(name)} {escape_html(str(name).title())}: "
        f"<b>{format_number(item['count'])}</b> · {format_size(item['size'])}"
        for name, item in sorted((stats.get("by_category") or {}).items())
    )
    return "\n".join(
        [
            f"{Emoji.STATS} <b>Storage statistics</b>",
            "",
            f"{storage_status_emoji(used, quota)} <code>{progress_bar(used, quota or 1, width=14)}</code>",
            f"{key_value_line('Used', human_size_pair(used, quota))}",
            f"{key_value_line('Free', format_size(user.free_space(quota)))}",
            f"{key_value_line('Files', format_number(int(stats.get('files') or 0)))}",
            f"{key_value_line('Folders', format_number(int(stats.get('folders') or 0)))}",
            f"{key_value_line('Public files', format_number(int(stats.get('public_files') or 0)))}",
            f"{key_value_line('Max file size', format_size(max_file_size))}",
            "",
            f"<b>By type</b>{rows or '  —'}",
            "",
            f"{Emoji.CLOCK} Measured {escape_html(format_date(_parse_iso(stats.get('generated_at'))))} UTC",
        ]
    )


# ---------------------------------------------------------------------------
# Errors & misc
# ---------------------------------------------------------------------------
def not_owner() -> str:
    return f"{Emoji.LOCK} <b>Access denied.</b>\nThis file belongs to another user."


def error_generic(message: str | None = None) -> str:
    """Catch-all error message; the technical detail is logged, not shown."""
    detail = escape_html(message) if message else "Something went wrong on our side."
    return f"{Emoji.ERROR} <b>{detail}</b>\n{Emoji.REFRESH} Please try again in a moment."


def operation_cancelled() -> str:
    return f"{Emoji.CLOSE} Operation cancelled."


def admin_only() -> str:
    return f"{Emoji.LOCK} This action could not be completed."


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _toggle_icon(value: Any) -> str:
    """Green tick / red cross for a boolean preference row."""
    return (Emoji.CHECK if value else Emoji.CROSS).plain()


def _quota_of(user: User) -> int:
    """Quota for display purposes (0 -> unlimited rendering)."""
    return user.storage_quota or 0


def _parse_iso(value: Any):
    """Parse an ISO timestamp coming from the stats payload."""
    from datetime import datetime

    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return value


# ---------------------------------------------------------------------------
# Legacy-bot helpers (category menu, file captions, password gate).
# ``bot/texts_uz.py`` overrides these when APP_LANGUAGE=uz; the English defaults
# keep the module importable and the handlers language-agnostic.
# ---------------------------------------------------------------------------
CATEGORY_TITLES: dict[str, str] = {
    "video": "🎬 Videos",
    "photo": "🖼️ Photos",
    "apps": "🤖 APK/IPA",
    "audio": "🎵 Audio",
    "document": "📑 Documents",
    "archive": "🗜️ Archives",
    "other": "📄 Others",
    "all": "⭐ All files",
    "pinned": "📌 Important",
}


def category_title(key: str) -> str:
    """Localised title of a legacy category tab."""
    return CATEGORY_TITLES.get(key, CATEGORY_TITLES["all"])


def category_header(key: str, page: int, page_size: int, total: int) -> str:
    """``<b>🎬 Videos</b> — 1-5 shown (of 42):`` style header."""
    if total == 0:
        return f"<b>{category_title(key)}</b> — nothing here yet."
    start = page * page_size + 1
    end = min(total, (page + 1) * page_size)
    return f"<b>{category_title(key)}</b> — showing {start}-{end} of {total}:"


def file_caption(record: File, *, folder_name: str = "umumiy") -> str:
    """Caption rendered under a file when browsing (legacy layout)."""
    from bot.utils.formatters import format_date

    pin_icon = "📌 " if record.is_pinned else ""
    icon = Emoji.for_category(record.category)
    mb = round(record.file_size / 1024 / 1024, 2)
    date = format_date(record.created_at, fmt="%Y-%m-%d %H:%M")
    return (
        f"{pin_icon}{icon} <b>{escape_html(record.file_name)}</b>\n"
        f"💾 {mb} MB  |  📅 {date}\n"
        f"📁 {escape_html(folder_name)}"
    )


def no_files_yet() -> str:
    """Empty-cloud hint."""
    return "😔 Nothing stored yet.\n📤 Send me a file — it will be saved!"


def password_prompt() -> str:
    """🔐 Password gate prompt."""
    return f"{Emoji.LOCK} <b>Enter the password:</b>"


def password_wrong(attempts_left: int) -> str:
    """Wrong password reply with the remaining attempts."""
    tail = f"\n{Emoji.INFO} Attempts left: {attempts_left}" if attempts_left >= 0 else ""
    return f"{Emoji.CROSS} <b>Wrong password!</b> Try again.{tail}"


def password_locked(retry_after: int) -> str:
    """Cooldown reply after too many wrong attempts."""
    return (
        f"{Emoji.HOURGLASS} <b>Too many attempts.</b>\n"
        f"Try again in {retry_after} seconds."
    )


def password_ok() -> str:
    """Successful gate pass."""
    return f"{Emoji.CHECK} <b>Welcome!</b>\n\nPick an option from the menu:"


def auth_required() -> str:
    """Sent when an unauthorized user presses a button."""
    return f"{Emoji.LOCK} Please sign in with <b>/start</b> first!"


def pin_toggled(record: File) -> str:
    """📌 Pin state changed."""
    state = "marked as important" if record.is_pinned else "unpinned"
    return f"{Emoji.PIN} <b>{escape_html(truncate_middle(record.file_name, 42))}</b> {state}!"


def search_empty() -> str:
    """No search results."""
    return f"{Emoji.SEARCH} Nothing found."


def pass_code_wrong() -> str:
    """Wrong share access code."""
    return f"{Emoji.KEY} <b>Wrong access code.</b>\nOpen the link again and retry."


def file_uuid_short(record: File) -> str:
    """Compact id used inside callback data (fits the 64-byte limit)."""
    return uuid.UUID(str(record.id)).hex
