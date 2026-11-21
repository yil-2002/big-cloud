"""``Files`` table - a Telegram message stored in the user's cloud."""

from __future__ import annotations

import enum
import re
import uuid
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    ColumnElement,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.database import Base
from db.models.base import TimestampMixin

if TYPE_CHECKING:  # pragma: no cover
    from db.models.folder import Folder
    from db.models.shared_link import SharedLink
    from db.models.user import User


class Visibility(str, enum.Enum):
    """Access level of a stored file."""

    PRIVATE = "private"    # only the owner (and their signed URLs) can fetch it
    UNLISTED = "unlisted"  # reachable by link/token, hidden from listings
    PUBLIC = "public"      # listed in public search + reachable by token


class FileCategory(str, enum.Enum):
    """Coarse media type used for icons, inline result kinds and players."""

    PHOTO = "photo"
    VIDEO = "video"
    AUDIO = "audio"
    VOICE = "voice"
    ANIMATION = "animation"
    DOCUMENT = "document"
    ARCHIVE = "archive"
    #: Android package - first-class category in the legacy bot.
    APK = "apk"
    #: iOS package - first-class category in the legacy bot.
    IPA = "ipa"
    OTHER = "other"


#: Extension -> category mapping (fast, avoids trusting client MIME types).
_CATEGORY_BY_EXTENSION: dict[str, FileCategory] = {
    # images
    "jpg": FileCategory.PHOTO, "jpeg": FileCategory.PHOTO, "png": FileCategory.PHOTO,
    "gif": FileCategory.ANIMATION, "webp": FileCategory.PHOTO, "bmp": FileCategory.PHOTO,
    "svg": FileCategory.PHOTO, "heic": FileCategory.PHOTO,
    # video
    "mp4": FileCategory.VIDEO, "mkv": FileCategory.VIDEO, "mov": FileCategory.VIDEO,
    "avi": FileCategory.VIDEO, "webm": FileCategory.VIDEO, "m4v": FileCategory.VIDEO,
    "flv": FileCategory.VIDEO, "wmv": FileCategory.VIDEO, "ts": FileCategory.VIDEO,
    # audio
    "mp3": FileCategory.AUDIO, "flac": FileCategory.AUDIO, "wav": FileCategory.AUDIO,
    "ogg": FileCategory.AUDIO, "m4a": FileCategory.AUDIO, "aac": FileCategory.AUDIO,
    "opus": FileCategory.AUDIO, "wma": FileCategory.AUDIO,
    # voice notes
    "oga": FileCategory.VOICE, "amr": FileCategory.VOICE,
    # mobile packages (legacy bot treated these as a top-level menu entry)
    "apk": FileCategory.APK, "xapk": FileCategory.APK, "apks": FileCategory.APK,
    "ipa": FileCategory.IPA,
    # archives
    "zip": FileCategory.ARCHIVE, "rar": FileCategory.ARCHIVE, "7z": FileCategory.ARCHIVE,
    "tar": FileCategory.ARCHIVE, "gz": FileCategory.ARCHIVE, "bz2": FileCategory.ARCHIVE,
    "xz": FileCategory.ARCHIVE,
}

_CATEGORY_BY_MIME_PREFIX: dict[str, FileCategory] = {
    "image/": FileCategory.PHOTO,
    "video/": FileCategory.VIDEO,
    "audio/": FileCategory.AUDIO,
    "application/zip": FileCategory.ARCHIVE,
    "application/x-7z": FileCategory.ARCHIVE,
    "application/x-rar": FileCategory.ARCHIVE,
    "application/gzip": FileCategory.ARCHIVE,
    "application/x-tar": FileCategory.ARCHIVE,
    "application/vnd.android.package-archive": FileCategory.APK,
    "application/octet-stream": FileCategory.OTHER,
}

#: Mobile packages - the legacy bot exposed them as one "APK/IPA" menu entry.
_APP_CATEGORIES: tuple[FileCategory, ...] = (FileCategory.APK, FileCategory.IPA)

#: Categories the Mini App can play inline (``<video>``/``<audio>`` tags).
_STREAMABLE_CATEGORIES: tuple[FileCategory, ...] = (
    FileCategory.VIDEO,
    FileCategory.AUDIO,
    FileCategory.VOICE,
    FileCategory.ANIMATION,
)

#: Characters Telegram/HTTP handle poorly in file names.
_UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class File(Base, TimestampMixin):
    """A single stored object.

    Telegram keeps the payload itself: ``file_id`` is the permanent handle used
    by ``sendDocument``/``getFile``, ``file_unique_id`` identifies the same bytes
    across re-uploads (used for de-duplication).
    """

    __tablename__ = "files"
    __table_args__ = (
        # A user can store the very same Telegram file only once (de-dup).
        UniqueConstraint("user_id", "file_unique_id", name="uq_file_user_unique_id"),
        Index("ix_files_user_folder", "user_id", "folder_id"),
        Index("ix_files_user_created", "user_id", "created_at"),
        Index("ix_files_file_id", "file_id"),
        Index("ix_files_name_search", "file_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # NOTE: the legacy bot used an integer primary key (``files.id SERIAL``) in
    # its callback grammar (``pin:12``). Here the UUID below is the primary key
    # and callbacks carry its 32-char ``hex`` form, which keeps the same grammar
    # (``pin:<id>``) within Telegram's 64-byte limit and works on both SQLite
    # and PostgreSQL without a sequence (SQLite has no sequence support).
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: ``NULL`` == file lives in the user's root.
    folder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("folders.id", ondelete="SET NULL"), nullable=True
    )

    # --- Telegram handles ----------------------------------------------------
    file_id: Mapped[str] = mapped_column(String(255), nullable=False)
    file_unique_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # --- metadata ------------------------------------------------------------
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    category: Mapped[FileCategory] = mapped_column(
        Enum(FileCategory, name="file_category", native_enum=False, length=16),
        default=FileCategory.OTHER,
        nullable=False,
    )
    #: Free-text description shown in the Mini App.
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Media duration in seconds (video/audio/voice) when Telegram reports it.
    duration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: ``file_id`` of a small preview (photo thumbnail / video thumbnail).
    thumbnail_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: 🗑 Savat (soft delete): ``NULL`` = faol, aks holda o'chirilgan vaqt.
    #: DELETE endi qatorni o'chirmaydi — shu maydonni to'ldiradi; ro'yxatlar
    #: uni filtrlaydi, ``restore`` esa NULL'ga qaytaradi. Yakuniy tozalash
    #: (purge) alohida va aniq amal.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- sharing / stats -----------------------------------------------------
    #: Kept for backward compatibility with the ``is_public`` boolean API.
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    visibility: Mapped[Visibility] = mapped_column(
        Enum(Visibility, name="file_visibility", native_enum=False, length=16),
        default=Visibility.PRIVATE,
        nullable=False,
    )
    views: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    downloads: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: "Muhim fayllar" of the legacy bot - pinned items get their own menu tab.
    # --- private archive vault (persistence copy) ----------------------------
    #: Chat the vault copy lives in (``ARCHIVE_CHANNEL_ID``).
    archive_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: ``message_id`` of the copy - lets us edit/delete it later.
    archive_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: ``file_id`` Telegram assigned *inside the channel*. This is the resilient
    #: reference: it keeps working even when the user deletes the original
    #: message, because the channel copy is a separate object.
    archive_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    is_pinned: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True
    )

    # --- relationships -------------------------------------------------------
    owner: Mapped[User] = relationship("User", back_populates="files", lazy="joined")
    folder: Mapped[Folder | None] = relationship("Folder", back_populates="files", lazy="joined")
    shared_links: Mapped[list[SharedLink]] = relationship(
        "SharedLink",
        back_populates="file",
        cascade="all, delete-orphan",
        passive_deletes=True,
        # ``noload``: never trigger implicit IO in async code. Load explicitly
        # with ``selectinload(File.shared_links)`` where the links are needed.
        lazy="noload",
        order_by="desc(SharedLink.created_at)",
    )

    # --- computed properties -------------------------------------------------
    @hybrid_property
    def extension(self) -> str:
        """Lowercase file extension without the dot (``""`` when absent)."""
        return PurePosixPath(self.file_name).suffix.lstrip(".").lower()

    @hybrid_property
    def is_streamable(self) -> bool:
        """``True`` when the Mini App can play it inline (audio/video)."""
        return self.category in _STREAMABLE_CATEGORIES

    @is_streamable.expression
    def is_streamable(cls) -> ColumnElement[bool]:
        """SQL form: ``category IN ('video','audio','voice','animation')``."""
        return cls.category.in_(_STREAMABLE_CATEGORIES)

    @hybrid_property
    def is_app(self) -> bool:
        """``True`` for APK/IPA packages (the legacy "🤖 APK/IPA" menu)."""
        return self.category in _APP_CATEGORIES

    @is_app.expression
    def is_app(cls) -> ColumnElement[bool]:
        """SQL form: ``category IN ('apk','ipa')``."""
        return cls.category.in_(_APP_CATEGORIES)

    @property
    def is_image(self) -> bool:
        """``True`` for pictures (drives inline ``InlineQueryResultPhoto``)."""
        return self.category is FileCategory.PHOTO

    @property
    def has_active_link(self) -> bool:
        """``True`` when an unexpired share link is *already loaded* in memory.

        ``shared_links`` uses ``lazy="noload"`` so this never performs hidden IO
        inside the event loop; use ``ShareService.get_active_token()`` for a
        database-backed lookup.
        """
        links = self.__dict__.get("shared_links") or []
        return any(link.is_active_now for link in links)

    # --- helpers -------------------------------------------------------------
    @classmethod
    def detect_category(cls, file_name: str, mime_type: str | None = None) -> FileCategory:
        """Infer :class:`FileCategory` from extension first, MIME type second."""
        extension = PurePosixPath(file_name or "").suffix.lstrip(".").lower()
        if extension in _CATEGORY_BY_EXTENSION:
            return _CATEGORY_BY_EXTENSION[extension]
        if mime_type:
            lowered = mime_type.lower()
            for prefix, category in _CATEGORY_BY_MIME_PREFIX.items():
                if lowered.startswith(prefix):
                    return category
        return FileCategory.OTHER

    @staticmethod
    def sanitize_filename(name: str, *, fallback: str = "file") -> str:
        """Make a file name safe for HTTP headers and cross-platform storage."""
        cleaned = _UNSAFE_FILENAME.sub("_", (name or "").strip()).strip(". ")
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        return (cleaned or fallback)[:512]

    def rename(self, new_name: str) -> str:
        """Rename the file, keeping its extension when the user dropped it."""
        safe = self.sanitize_filename(new_name)
        current_ext = PurePosixPath(self.file_name).suffix
        if current_ext and not PurePosixPath(safe).suffix:
            safe = f"{safe}{current_ext}"
        self.file_name = safe
        self.category = self.detect_category(safe, self.mime_type)
        return self.file_name

    # --- archive vault -------------------------------------------------------
    @property
    def is_archived(self) -> bool:
        """``True`` once the persistence copy exists in the archive channel."""
        return bool(self.archive_message_id or self.archive_file_id)

    #: The channel copy is preferred for playback: it cannot be revoked by the
    #: user deleting their own message.
    @property
    def best_file_id(self) -> str:
        """``archive_file_id`` when present, else the original ``file_id``."""
        return self.archive_file_id or self.file_id

    def mark_archived(
        self,
        *,
        chat_id: str | int,
        message_id: int,
        file_id: str | None = None,
    ) -> None:
        """Record the vault copy produced by :class:`services.vault.ArchiveVault`."""
        self.archive_chat_id = str(chat_id)
        self.archive_message_id = int(message_id)
        if file_id:
            self.archive_file_id = file_id
        self.archived_at = datetime.now(UTC)

    def toggle_pin(self) -> bool:
        """Flip the "muhim" flag and return the new value."""
        self.is_pinned = not bool(self.is_pinned)
        return self.is_pinned

    def set_visibility(self, visibility: Visibility) -> None:
        """Change visibility keeping the legacy ``is_public`` flag consistent."""
        self.visibility = visibility
        self.is_public = visibility is Visibility.PUBLIC

    def to_dict(self, *, include_private: bool = True) -> dict[str, object]:
        """JSON-serialisable representation consumed by the REST API."""
        payload: dict[str, object] = {
            "id": str(self.id),
            "name": self.file_name,
            "size": self.file_size,
            "mime_type": self.mime_type,
            "category": self.category.value,
            "visibility": self.visibility.value,
            "is_public": self.is_public,
            "views": self.views,
            "downloads": self.downloads,
            "is_pinned": self.is_pinned,
            "duration": self.duration,
            "width": self.width,
            "height": self.height,
            "folder_id": str(self.folder_id) if self.folder_id else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "is_streamable": self.is_streamable,
        }
        if include_private:
            payload.update(
                {
                    "file_id": self.file_id,
                    "file_unique_id": self.file_unique_id,
                    "caption": self.caption,
                    "thumbnail_file_id": self.thumbnail_file_id,
                }
            )
        return payload

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<File id={self.id} name={self.file_name!r} size={self.file_size} "
            f"visibility={self.visibility.value}>"
        )
