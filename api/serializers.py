"""ORM row -> Pydantic schema conversion.

Centralised so every endpoint renders identical payloads (including computed
fields such as ``storage_free``, ``path`` or ``stream_url``) and so the routes
stay focused on HTTP concerns.
"""

from __future__ import annotations

import uuid
from typing import Any

from api.schemas import (
    CategoryBreakdown,
    FileOut,
    FolderOut,
    PublicFileOut,
    ShareOut,
    StatsOut,
    UserOut,
)
from config import Settings
from db.models import File, Folder, SharedLink, User
from services.filetype import effective_type, is_streamable_category
from services.share import ShareService

__all__ = [
    "serialize_file",
    "serialize_folder",
    "serialize_share",
    "serialize_stats",
    "serialize_user",
]


def _format_size(value: int) -> str:
    """Compact human size for ``quota_label`` (1073741824 -> "1.0 GB")."""
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    size = float(value or 0)
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    return f"{size:.0f} {units[index]}" if index == 0 or size >= 10 else f"{size:.1f} {units[index]}"


def serialize_user(user: User, settings: Settings, *, is_admin: bool | None = None) -> UserOut:
    """Render a user with effective quota numbers."""
    quota = user.quota(settings.storage.default_quota)
    used = int(user.storage_used or 0)
    free = user.free_space(settings.storage.default_quota)
    admin = (
        user.telegram_id in settings.bot.admin_ids if is_admin is None else is_admin
    )
    unlimited = quota <= 0
    return UserOut(
        telegram_id=user.telegram_id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        language_code=user.language_code,
        is_premium=bool(user.is_premium),
        is_admin=admin,
        login=user.login,
        is_authenticated=bool(user.is_authenticated),
        is_blocked=bool(user.is_blocked),
        full_name=user.display_name,
        storage_used=used,
        storage_quota=quota,
        unlimited=unlimited,
        quota_label=settings.storage.unlimited_label if unlimited else _format_size(quota),
        storage_free=0 if unlimited else free,
        percent_used=0.0 if unlimited else round((used / quota) * 100, 2),
        settings=dict(user.settings or {}),
        created_at=user.created_at,
    )


def serialize_share(link: SharedLink, share: ShareService) -> ShareOut:
    """Render a share link including both entry-point URLs."""
    return ShareOut(
        id=link.id,
        token=link.token,
        file_id=link.file_id,
        expires_at=link.expires_at,
        seconds_left=link.seconds_left,
        is_active=link.is_active_now,
        has_pass_code=link.has_pass_code,
        views=int(link.views or 0),
        downloads=int(link.downloads or 0),
        created_at=link.created_at,
        deep_link=share.deep_link(link.token),
        web_url=share.share_url(link.token),
    )


def serialize_public_file(record: File, link: SharedLink | None = None) -> PublicFileOut:
    """Render the anonymous view of a shared file.

    Deliberately leaks nothing sensitive: no ``file_id``, no owner id, no folder -
    only what a recipient needs to recognise and play the media.
    """
    owner_name: str | None = None
    if record.owner is not None:
        owner_name = record.owner.display_name

    mime, category = effective_type(record)
    return PublicFileOut(
        id=record.id,
        name=record.file_name,
        size=int(record.file_size or 0),
        category=category.value,
        mime_type=mime,
        is_streamable=is_streamable_category(category),
        duration=record.duration,
        width=record.width,
        height=record.height,
        caption=record.caption,
        views=int(record.views or 0),
        downloads=int(record.downloads or 0),
        created_at=record.created_at,
        owner_name=owner_name,
        requires_pass_code=bool(link.has_pass_code) if link is not None else False,
        expires_at=link.expires_at if link is not None else None,
    )


def serialize_file(
    record: File,
    *,
    settings: Settings,
    share: ShareService,
    telegram_id: int,
    links: list[SharedLink] | None = None,
    with_stream: bool = True,
) -> FileOut:
    """Render a file for its owner (optionally with a signed playback URL)."""
    owned = record.user_id == telegram_id or telegram_id in settings.bot.admin_ids
    rendered_links = [
        serialize_share(link, share)
        for link in (links if links is not None else list(record.shared_links or []))
    ]

    # Tur har doim aniq bo'lishi shart: yozuvda ``octet-stream``/bo'sh mime yoki
    # "other" kategoriya qolgan bo'lsa (masalan, kengaytmasiz ``tmp_3295``),
    # kengaytma/mime bo'yicha qayta hisoblaymiz. Aks holda Mini App pleer
    # ochilmaydi. Bu yerda **disk o'qilmaydi** — ro'yxat so'rovlari tez qoladi.
    mime, category = effective_type(record)
    streamable = is_streamable_category(category)

    stream_url: str | None = None
    download_url: str | None = None
    if owned and with_stream and streamable:
        ticket = share.issue_stream_ticket(record.id, telegram_id, purpose="stream")
        stream_url = share.stream_url(ticket)
        download_url = share.stream_url(ticket, download=True)

    # Thumbnail: xuddi shu /stream endpointi, lekin ``purpose="thumb"`` —
    # server asosiy fayl o'rniga kichik rasmni oqizadi. Ro'yxat sahifasi uchun
    # ham beriladi (``with_stream`` dan mustaqil): karta rasmlari doim kerak,
    # thumbnail'lar kichik bo'lgani uchun 20 MB devori ularga tegmaydi.
    thumbnail_url: str | None = None
    if owned and record.thumbnail_file_id:
        thumb_ticket = share.issue_stream_ticket(record.id, telegram_id, purpose="thumb")
        thumbnail_url = share.stream_url(thumb_ticket)

    return FileOut(
        id=record.id,
        name=record.file_name,
        size=int(record.file_size or 0),
        mime_type=mime,
        category=category.value,
        visibility=record.visibility.value,
        is_public=bool(record.is_public),
        is_streamable=streamable,
        views=int(record.views or 0),
        downloads=int(record.downloads or 0),
        duration=record.duration,
        width=record.width,
        height=record.height,
        caption=record.caption,
        folder_id=record.folder_id,
        is_pinned=bool(record.is_pinned),
        archived=bool(record.is_archived),
        archive_message_id=record.archive_message_id,
        archived_at=record.archived_at,
        file_id=record.file_id if owned else None,
        created_at=record.created_at,
        updated_at=record.updated_at,
        links=rendered_links,
        stream_url=stream_url,
        download_url=download_url,
        thumbnail_url=thumbnail_url,
        deleted_at=record.deleted_at,
    )


def serialize_folder(
    folder: Folder,
    *,
    files_count: int = 0,
    folders_count: int = 0,
    size: int = 0,
) -> FolderOut:
    """Render a folder with its breadcrumb path and counters."""
    return FolderOut(
        id=folder.id,
        name=folder.name,
        parent_id=folder.parent_id,
        path=folder.breadcrumb(),
        files_count=files_count,
        folders_count=folders_count,
        size=size,
        created_at=folder.created_at,
    )


def serialize_stats(stats: dict[str, Any]) -> StatsOut:
    """Render the aggregate statistics payload."""
    raw_categories: dict[str, dict[str, int]] = stats.get("by_category") or {}
    return StatsOut(
        files=int(stats.get("files") or 0),
        folders=int(stats.get("folders") or 0),
        public_files=int(stats.get("public_files") or 0),
        used=int(stats.get("used") or 0),
        quota=int(stats.get("quota") or 0),
        free=int(stats.get("free") or 0),
        percent_used=float(stats.get("percent_used") or 0.0),
        by_category={
            name: CategoryBreakdown(
                count=int(item.get("count") or 0), size=int(item.get("size") or 0)
            )
            for name, item in raw_categories.items()
        },
        generated_at=stats.get("generated_at"),
    )


def as_uuid(value: str | uuid.UUID) -> uuid.UUID:
    """Coerce a path/query parameter into :class:`uuid.UUID`."""
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(value)
