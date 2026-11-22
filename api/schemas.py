"""Pydantic request/response models for the REST API.

Every model is intentionally explicit: the OpenAPI schema at ``/docs`` is the
contract the Mini App is generated against, so optional fields carry defaults and
no model leaks ORM internals (``file_id`` is only exposed to the owner).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "AuthResponse",
    "CategoryBreakdown",
    "ErrorBody",
    "ErrorResponse",
    "FileBulkDelete",
    "FileCreate",
    "FileOut",
    "FilePage",
    "FileUpdate",
    "FolderCreate",
    "FolderOut",
    "FolderUpdate",
    "LoginRequest",
    "MessageResponse",
    "Page",
    "PublicFileOut",
    "RegisterRequest",
    "ShareCreate",
    "ShareOut",
    "ShareUnlock",
    "StatsOut",
    "StreamOut",
    "UserOut",
    "UserSettingsPatch",
]

T = TypeVar("T")


class _Model(BaseModel):
    """Base with the project-wide Pydantic configuration."""

    model_config = ConfigDict(
        from_attributes=True,      # build straight from ORM rows
        populate_by_name=True,
        extra="ignore",
        # UUID/datetime are serialised natively by Pydantic v2 in JSON mode,
        # so no custom ``json_encoders`` (deprecated) are needed.
    )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
class TelegramAuthRequest(_Model):
    """Body of ``POST /auth/telegram`` (Mini App handshake)."""

    init_data: str = Field(
        min_length=16,
        max_length=8192,
        description="Raw ``WebApp.initData`` string, signature-verified server-side.",
        examples=["user=%7B%22id%22%3A1%7D&auth_date=1&hash=abc"],
    )
    #: When true, a session is created even for brand-new users (default).
    create_if_missing: bool = True


class LoginRequest(_Model):
    """``POST /auth/login`` — hugecloud hisobi bilan kirish."""

    login: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class RegisterRequest(_Model):
    """``POST /auth/register`` — yangi hisob ochish."""

    login: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_.]{3,32}$")
    password: str = Field(min_length=6, max_length=128)


class AuthResponse(_Model):
    """Issued session token plus the user's current state."""

    access_token: str
    #: The Mini App reads either spelling; keeping both avoids a client update.
    token: str | None = None
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(description="Token lifetime in seconds")
    user: UserOut


class UserSettingsPatch(_Model):
    """Partial update of the ``users.settings`` JSON column."""

    theme: Literal["dark", "light", "auto"] | None = None
    view_mode: Literal["list", "grid"] | None = None
    sort_by: Literal["created_at", "name", "size", "views"] | None = None
    sort_desc: bool | None = None
    public_by_default: bool | None = None
    inline_enabled: bool | None = None
    notify_uploads: bool | None = None
    language: str | None = Field(default=None, max_length=8)

    def as_patch(self) -> dict[str, Any]:
        """Drop ``None`` values so partial updates never wipe a preference."""
        return {key: value for key, value in self.model_dump().items() if value is not None}


class UserOut(_Model):
    """Public projection of a user row."""

    telegram_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    language_code: str | None = None
    is_premium: bool = False
    is_admin: bool = False
    #: hugecloud hisobi: login nomi va autentifikatsiya holati.
    login: str | None = None
    is_authenticated: bool = False
    is_blocked: bool = False
    full_name: str | None = None
    storage_used: int = 0
    storage_quota: int = Field(description="Effective quota in bytes (0 = unlimited)")
    #: ``True`` when the quota is unlimited - the UI then shows "Cheksiz".
    unlimited: bool = False
    quota_label: str = "Unlimited"
    storage_free: int = 0
    percent_used: float = 0.0
    settings: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
class FileOut(_Model):
    """File metadata for the owner."""

    id: uuid.UUID
    name: str
    size: int
    mime_type: str | None = None
    category: str
    visibility: str
    is_public: bool = False
    is_streamable: bool = False
    views: int = 0
    downloads: int = 0
    duration: int | None = None
    width: int | None = None
    height: int | None = None
    caption: str | None = None
    folder_id: uuid.UUID | None = None
    #: 📌 "muhim" belgisi (legacy pin).
    is_pinned: bool = False
    #: 🛡️ Arxiv kanali nusxasi mavjudmi (file persistence).
    archived: bool = False
    archive_message_id: int | None = None
    archived_at: datetime | None = None
    file_id: str | None = Field(default=None, description="Telegram handle (owner only)")
    created_at: datetime | None = None
    updated_at: datetime | None = None
    links: list[ShareOut] = Field(default_factory=list)
    stream_url: str | None = Field(
        default=None, description="Signed short-lived playback URL (streamable files only)"
    )
    download_url: str | None = Field(
        default=None, description="Same ticket with Content-Disposition: attachment"
    )
    #: Kichik rasm (Telegram thumbnail) uchun imzolangan URL — kartadagi
    #: ``<img>`` shu manzildan yuklanadi. Thumbnail bo'lmasa ``None`` (frontend
    #: emoji fallback ko'rsatadi).
    thumbnail_url: str | None = Field(
        default=None, description="Signed URL of the Telegram thumbnail (None = no preview)"
    )
    #: 🗑 Savat vaqti: ``None`` = faol fayl; qiymat bo'lsa fayl savatda.
    deleted_at: datetime | None = Field(
        default=None, description="Set while the file sits in the trash (soft delete)"
    )


class PublicFileOut(_Model):
    """What an anonymous visitor may see about a shared file."""

    id: uuid.UUID
    name: str
    size: int
    category: str
    mime_type: str | None = None
    is_streamable: bool = False
    duration: int | None = None
    width: int | None = None
    height: int | None = None
    caption: str | None = None
    views: int = 0
    downloads: int = 0
    created_at: datetime | None = None
    owner_name: str | None = None
    requires_pass_code: bool = False
    expires_at: datetime | None = None
    stream_url: str | None = None
    download_url: str | None = None


class FileCreate(_Model):
    """Register a file the bot already stored (Mini App hand-off)."""

    file_id: str = Field(min_length=4, max_length=255)
    file_unique_id: str = Field(min_length=4, max_length=64)
    file_name: str = Field(min_length=1, max_length=512)
    file_size: int = Field(ge=0)
    mime_type: str | None = Field(default=None, max_length=128)
    folder_id: uuid.UUID | None = None
    caption: str | None = Field(default=None, max_length=1024)
    duration: int | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    #: Telegram thumbnail ``file_id`` (video/photo/document preview'lari uchun).
    thumbnail_file_id: str | None = Field(default=None, max_length=255)
    is_public: bool = False

    @field_validator("file_name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        """Reject blank names early with a clear message."""
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("file_name must not be blank")
        return cleaned


class FileUpdate(_Model):
    """Rename / move / change visibility (all fields optional)."""

    name: str | None = Field(default=None, min_length=1, max_length=512)
    folder_id: uuid.UUID | None = None
    #: ``move_to_root`` distinguishes "no change" from "move to root".
    move_to_root: bool = False
    is_public: bool | None = None
    caption: str | None = Field(default=None, max_length=1024)


class Page(_Model, Generic[T]):
    """Generic pagination envelope."""

    items: list[T] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
    pages: int = 1

    @property
    def has_next(self) -> bool:
        """``True`` when another page exists."""
        return self.page < self.pages

    @property
    def has_prev(self) -> bool:
        """``True`` when a previous page exists."""
        return self.page > 1


class FilePage(Page[FileOut]):
    """Paginated file list."""


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------
class FolderOut(_Model):
    id: uuid.UUID
    name: str
    parent_id: uuid.UUID | None = None
    path: str = Field(description="Breadcrumb such as ``Docs / 2026``")
    files_count: int = 0
    folders_count: int = 0
    size: int = 0
    created_at: datetime | None = None


class FolderCreate(_Model):
    name: str = Field(min_length=1, max_length=255)
    parent_id: uuid.UUID | None = None

    @field_validator("name")
    @classmethod
    def _clean(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Folder name must not be blank")
        return cleaned


class FolderUpdate(_Model):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    parent_id: uuid.UUID | None = None
    move_to_root: bool = False


# ---------------------------------------------------------------------------
# Sharing
# ---------------------------------------------------------------------------
class ShareCreate(_Model):
    """Create a share link."""

    ttl_seconds: int | None = Field(
        default=None, ge=0, le=365 * 86400, description="0 or null = use the server default"
    )
    pass_code: str | None = Field(
        default=None, min_length=4, max_length=32, description="Optional access code"
    )
    generate_pass_code: bool = Field(
        default=False, description="Let the server generate a numeric code"
    )
    make_public: bool = False


class ShareOut(_Model):
    id: uuid.UUID
    token: str
    #: Clear-text access code, returned **once** at creation time (only the hash
    #: is stored), so the owner can forward it to the recipient.
    pass_code: str | None = None
    file_id: uuid.UUID
    expires_at: datetime | None = None
    seconds_left: int | None = None
    is_active: bool = True
    has_pass_code: bool = False
    views: int = 0
    downloads: int = 0
    created_at: datetime | None = None
    deep_link: str | None = Field(default=None, description="t.me/<bot>?start=<token>")
    web_url: str | None = Field(default=None, description="{BASE_URL}/s/<token>")


class FileBulkDelete(_Model):
    """Body of ``POST /files/bulk-delete``."""

    ids: list[uuid.UUID] = Field(min_length=1, max_length=500)


class ShareUnlock(_Model):
    """Body of ``POST /public/{token}/unlock``."""

    pass_code: str = Field(min_length=1, max_length=32)


class StreamOut(_Model):
    """Signed playback URLs handed to the Mini App player."""

    stream_url: str
    download_url: str
    ticket_expires_in: int = Field(description="Ticket lifetime in seconds")
    name: str
    size: int
    category: str
    #: Aniqlangan MIME turi — Mini App ``<source type="...">`` uchun ishlatadi
    #: (kengaytmasiz ``tmp_3295`` fayllarda ham to'g'ri bo'lishi kerak).
    mime_type: str | None = None
    is_streamable: bool


# ---------------------------------------------------------------------------
# Stats & errors
# ---------------------------------------------------------------------------
class CategoryBreakdown(_Model):
    count: int = 0
    size: int = 0


class StatsOut(_Model):
    files: int = 0
    folders: int = 0
    public_files: int = 0
    used: int = 0
    quota: int = 0
    free: int = 0
    percent_used: float = 0.0
    by_category: dict[str, CategoryBreakdown] = Field(default_factory=dict)
    generated_at: datetime | None = None


class ErrorBody(_Model):
    code: str
    message: str
    details: dict[str, Any] | None = None


class ErrorResponse(_Model):
    """Uniform error envelope: ``{"error": {...}}``."""

    error: ErrorBody


class MessageResponse(_Model):
    """Simple acknowledgement payload."""

    ok: bool = True
    message: str = ""


class SessionDeviceOut(_Model):
    """Bitta faol sessiya (qurilma) haqidagi ma'lumot."""

    sid: str
    #: Joriy so'rov shu qurilmadan kelganmi (front-end "bu qurilma" deb belgilaydi).
    current: bool = False
    user_agent: str = ""
    ip: str = ""
    username: str = ""
    created_at: int = 0


class SessionDevicesOut(_Model):
    """``GET /auth/devices`` javobi: bitta akkaunt = bitta sessiya."""

    devices: list[SessionDeviceOut] = []
    #: Siyosat: yangi login eski qurilmani chiqarib yuboradi.
    single_session: bool = True


class ProfileOut(_Model):
    """Bitta profil (``GET /auth/profiles``). Parol hech qachon qaytarilmaydi."""

    login: str
    #: Hozir shu Telegram foydalanuvchisi shu profilda.
    active: bool = False
    #: ``/register`` bilan ochilgan qo'shimcha profil.
    extra: bool = False


class ProfilesOut(_Model):
    """Ko'p profilli sessiyalar: profillar ro'yxati va joriy profil."""

    items: list[ProfileOut] = []
    current: str | None = None
    #: Siyosat: profilga kirish oldingi sessiyani yakunlaydi.
    single_session: bool = True


# Forward references used by AuthResponse / FileOut.
AuthResponse.model_rebuild()
FileOut.model_rebuild()
