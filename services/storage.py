"""Core storage business logic: users, folders and files.

:class:`StorageService` is the only place that mutates storage data. Both the
aiogram handlers and the FastAPI routes depend on it, which guarantees that
quota accounting, de-duplication and cache invalidation behave identically no
matter how the request arrived.

All queries are built with SQLAlchemy 2.0 ``select()`` statements, collections
are loaded explicitly (``selectinload``/``joinedload``) and every write ends
with a cache invalidation for the affected user.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import Select, String, and_, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from config import Settings, get_settings
from db.models import (
    FILE_GROUPS,
    AuditLog,
    File,
    FileCategory,
    Folder,
    SharedLink,
    User,
)
from services.cache import Cache, get_cache
from services.exceptions import (
    DuplicateFileError,
    FileNotFoundError,
    FolderNotEmptyError,
    FolderNotFoundError,
    InsufficientQuotaError,
    NotOwnerError,
    TooLargeError,
    UserNotFoundError,
)
from services.filetype import detect_file, is_generic_mime

logger = logging.getLogger(__name__)

__all__ = ["GroupName", "SortField", "StorageService", "StoredFile"]

#: Sorting keys accepted by :meth:`StorageService.list_files`.
SortField = Literal["created_at", "name", "file_name", "size", "file_size", "views"]

#: Display groups of the ``/files`` menu: 🎬 Medialar, 🎵 Audiolar,
#: 📄 Hujjatlar, 📦 Arxivlar (+ the legacy 🤖 APK/IPA tab).
GroupName = Literal["media", "audio", "document", "archive", "apps", "all"]


@dataclass(slots=True)
class StoredFile:
    """Result of :meth:`StorageService.add_file`.

    ``is_new=False`` means the ``file_unique_id`` was **already stored**, so the
    existing row is returned instantly - no second upload, no duplicate record,
    no quota charged twice. Handlers branch on this flag to pick the copy
    ("Saqlandi" vs "Bu fayl allaqachon bulutda bor").
    """

    record: File
    is_new: bool = True
    #: ``True`` when the row already had an archive copy at registration time.
    deduplicated: bool = False

    def __bool__(self) -> bool:
        """A stored file is always truthy (``None`` is never returned)."""
        return True


class _Unset:
    """Sentinel distinguishing "no filter" from "filter by NULL folder"."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover
        return "<UNSET>"


_UNSET: Any = _Unset()
_SORTABLE_COLUMNS: dict[str, Any] = {
    "created_at": File.created_at,
    "name": File.file_name,
    "file_name": File.file_name,
    "size": File.file_size,
    #: API/Mini App "file_size" deb ham yuborishi mumkin — ikkalasi ham ishlaydi.
    "file_size": File.file_size,
    "views": File.views,
}


class StorageService:
    """User + folder + file operations."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        cache: Cache | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.cache = cache or get_cache()
        self.settings = settings or get_settings()

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------
    async def get_user(self, telegram_id: int, *, cached: bool = True) -> User | None:
        """Fetch a user, serving the cached projection when possible.

        The ORM instance is always returned (handlers need it); the cache is used
        to skip the round trip for hot users by storing the whole row as a dict
        and re-hydrating only the fields that matter for authorization.
        """
        if cached:
            snapshot = await self.cache.get(self.cache.keys.user(telegram_id))
            if snapshot and snapshot.get("_fresh"):
                user = await self.session.get(User, telegram_id)
                if user is not None:
                    return user
        user = await self.session.get(User, telegram_id)
        if user is not None:
            await self._cache_user(user)
        return user

    async def get_user_or_raise(self, telegram_id: int) -> User:
        """Like :meth:`get_user` but raises :class:`UserNotFoundError`."""
        user = await self.get_user(telegram_id)
        if user is None:
            raise UserNotFoundError(f"User {telegram_id} is not registered")
        return user

    async def get_or_create_user(
        self,
        telegram_id: int,
        *,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        language_code: str | None = None,
        is_premium: bool = False,
    ) -> tuple[User, bool]:
        """Idempotent registration. Returns ``(user, created)``."""
        user = await self.session.get(User, telegram_id)
        if user is not None:
            changed = self._sync_profile(user, username, first_name, last_name, language_code, is_premium)
            if changed:
                await self.session.flush()
                await self._cache_user(user)
            return user, False

        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            language_code=language_code,
            is_premium=is_premium,
        )
        # Admins get the larger quota out of the box.
        if telegram_id in self.settings.bot.admin_ids:
            user.storage_quota = self.settings.storage.admin_quota

        self.session.add(user)
        try:
            await self.session.flush()
        except IntegrityError:  # race between two concurrent updates
            await self.session.rollback()
            existing = await self.session.get(User, telegram_id)
            if existing is None:  # pragma: no cover - should not happen
                raise
            return existing, False

        await self.session.commit()
        await self._cache_user(user)
        logger.info("Registered new user %s (%s)", telegram_id, username)
        return user, True

    def _sync_profile(
        self,
        user: User,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
        language_code: str | None,
        is_premium: bool,
    ) -> bool:
        """Copy fresh Telegram profile data onto the row; report if changed."""
        changed = False
        for field, value in (
            ("username", username),
            ("first_name", first_name),
            ("last_name", last_name),
            ("language_code", language_code),
            ("is_premium", is_premium),
        ):
            if value is not None and getattr(user, field) != value:
                setattr(user, field, value)
                changed = True
        if user.is_blocked:
            user.is_blocked = False
            changed = True
        return changed

    async def update_settings(self, telegram_id: int, patch: dict[str, Any]) -> User:
        """Merge ``patch`` into ``users.settings`` (JSON column)."""
        user = await self.get_user_or_raise(telegram_id)
        merged = dict(user.settings or {})
        merged.update(patch)
        user.settings = merged
        # Re-assign so SQLAlchemy sees the mutation (JSON columns are not tracked
        # in place unless MutableDict is used).
        self.session.add(user)
        await self.session.commit()
        await self._cache_user(user)
        await self.cache.invalidate_user(telegram_id)
        return user

    # ------------------------------------------------------------------
    # Password gate (legacy BOT_PASSWORD behaviour)
    # ------------------------------------------------------------------
    @property
    def password_required(self) -> bool:
        """``True`` when a shared password is configured."""
        return bool(self.settings.bot.password_value())

    def is_authorized(self, user: User) -> bool:
        """Has this user passed the password gate (or is the gate disabled)?"""
        if not self.password_required:
            return True
        if user.telegram_id in self.settings.bot.admin_ids:
            return True
        return bool(user.is_authorized)

    async def authorize_with_password(self, telegram_id: int, password: str) -> bool:
        """Verify the shared password and persist the authorization.

        Uses a constant-time comparison; the flag is stored on the user row so a
        restart or redeploy does not ask for the password again (an improvement
        over the legacy in-memory ``authorized_users`` set).
        """
        import hmac as _hmac

        expected = self.settings.bot.password_value()
        if not expected:
            return True
        if not _hmac.compare_digest((password or "").strip(), expected):
            return False

        user = await self.get_user_or_raise(telegram_id)
        user.is_authorized = True
        self.session.add(user)
        await self.session.commit()
        await self._cache_user(user)
        logger.info("User %s passed the password gate", telegram_id)
        return True

    async def recalculate_storage(self, telegram_id: int) -> int:
        """Recompute ``users.storage_used`` from the ``files`` table."""
        total = await self.session.scalar(
            select(func.coalesce(func.sum(File.file_size), 0)).where(
                and_(File.user_id == telegram_id, File.deleted_at.is_(None))
            )
        )
        value = int(total or 0)
        await self.session.execute(
            update(User).where(User.telegram_id == telegram_id).values(storage_used=value)
        )
        await self.session.commit()
        await self.cache.delete(self.cache.keys.user(telegram_id))
        return value

    async def _cache_user(self, user: User) -> None:
        """Store a JSON projection of the user (settings + counters)."""
        await self.cache.set(
            self.cache.keys.user(user.telegram_id),
            {
                "telegram_id": user.telegram_id,
                "username": user.username,
                "first_name": user.first_name,
                "storage_used": user.storage_used,
                "storage_quota": user.storage_quota,
                "settings": user.settings,
                "is_premium": user.is_premium,
                "_fresh": True,
            },
            self.settings.cache.ttl_user,
        )

    # ------------------------------------------------------------------
    # Quota helpers
    # ------------------------------------------------------------------
    @property
    def default_quota(self) -> int:
        """Global per-user quota from settings (bytes)."""
        return self.settings.storage.default_quota

    def quota_for(self, user: User) -> int:
        """Effective quota for a user (per-user override wins). ``0`` = unlimited."""
        return user.quota(self.default_quota)

    def quota_label(self, user: User) -> str:
        """Human readable quota: ``"Cheksiz (Unlimited)"`` or ``"5 GB"``.

        Centralised so bot texts, the API and the Mini App all show the same
        string for the unlimited tier.
        """
        from bot.utils.formatters import format_size

        limit = self.quota_for(user)
        if limit <= 0:
            return self.settings.storage.unlimited_label
        return format_size(limit)

    def ensure_fits(self, user: User, size: int) -> None:
        """Raise :class:`TooLargeError`/:class:`InsufficientQuotaError` if needed."""
        max_size = self.settings.storage.max_file_size
        if max_size and size > max_size:
            raise TooLargeError(
                f"File is {size} bytes, the limit is {max_size} bytes",
                size=size,
                limit=max_size,
            )
        if not user.has_space(size, self.default_quota):
            raise InsufficientQuotaError(
                "Not enough free storage space",
                required=size,
                free=user.free_space(self.default_quota),
                quota=self.quota_for(user),
                used=user.storage_used,
            )

    # ------------------------------------------------------------------
    # Folders
    # ------------------------------------------------------------------
    async def create_folder(
        self, name: str, telegram_id: int, parent_id: uuid.UUID | None = None
    ) -> Folder:
        """Create a folder, validating the parent and duplicate sibling names."""
        name = name.strip()[:255]
        if not name:
            raise FolderNotFoundError("Folder name must not be empty")

        if parent_id is not None:
            await self.get_folder(parent_id, telegram_id)

        existing = await self.session.scalar(
            select(Folder).where(
                and_(
                    Folder.user_id == telegram_id,
                    Folder.parent_id == parent_id,
                    func.lower(Folder.name) == name.lower(),
                )
            )
        )
        if existing is not None:
            return existing

        folder = Folder(user_id=telegram_id, parent_id=parent_id, name=name)
        self.session.add(folder)
        try:
            await self.session.commit()
        except IntegrityError:  # unique constraint race
            await self.session.rollback()
            folder = await self.session.scalar(  # type: ignore[assignment]
                select(Folder).where(
                    and_(
                        Folder.user_id == telegram_id,
                        Folder.parent_id == parent_id,
                        func.lower(Folder.name) == name.lower(),
                    )
                )
            )
        await self.cache.invalidate_user(telegram_id)
        # ``parent`` munosabati yangi obyektda yuklanmagan bo'ladi; keyinroq
        # ``breadcrumb()`` unga sync kontekstda murojaat qilsa MissingGreenlet
        # yiqilishi bor edi (masalan, ``POST /folders`` + parent_id). Shu yerda
        # aniq qayta yuklaymiz — bitta qo'shimcha SELECT, lekin xavfsiz.
        if folder is not None and parent_id is not None:
            return await self.get_folder(folder.id, telegram_id)
        return folder

    async def get_folder(self, folder_id: uuid.UUID, telegram_id: int) -> Folder:
        """Fetch one folder owned by ``telegram_id`` (joined parent for paths)."""
        folder = await self.session.scalar(
            select(Folder)
            .where(and_(Folder.id == folder_id, Folder.user_id == telegram_id))
            .options(selectinload(Folder.parent))
        )
        if folder is None:
            raise FolderNotFoundError(f"Folder {folder_id} not found")
        return folder

    async def list_folders(
        self, telegram_id: int, parent_id: uuid.UUID | None = None
    ) -> Sequence[Folder]:
        """List the sub-folders of ``parent_id`` (``None`` == root), by name.

        ``parent`` munosabati oldindan yuklanadi: serializer ``breadcrumb()``
        ni sync kontekstda chaqiradi — lazy IO u yerda ``MissingGreenlet``
        bilan yiqilardi (masalan, ``GET /folders?parent_id=...``).
        """
        statement = (
            select(Folder)
            .where(and_(Folder.user_id == telegram_id, Folder.parent_id == parent_id))
            .options(selectinload(Folder.parent))
            .order_by(Folder.name)
        )
        return (await self.session.scalars(statement)).unique().all()

    async def rename_folder(self, folder_id: uuid.UUID, telegram_id: int, new_name: str) -> Folder:
        """Rename a folder (keeps the sibling-uniqueness rule)."""
        folder = await self.get_folder(folder_id, telegram_id)
        cleaned = new_name.strip()[:255]
        if not cleaned:
            raise FolderNotFoundError("Folder name must not be empty")
        folder.name = cleaned
        await self.session.commit()
        await self.cache.invalidate_user(telegram_id)
        return folder

    async def move_folder(
        self, folder_id: uuid.UUID, telegram_id: int, new_parent_id: uuid.UUID | None
    ) -> Folder:
        """Move a folder, refusing to create a cycle."""
        folder = await self.get_folder(folder_id, telegram_id)
        if new_parent_id is not None:
            if new_parent_id == folder_id:
                raise FolderNotEmptyError("A folder cannot be moved into itself")
            await self.get_folder(new_parent_id, telegram_id)
            if await folder.is_descendant_of(new_parent_id, self.session):
                raise FolderNotEmptyError("Cannot move a folder into its own sub-folder")
        folder.parent_id = new_parent_id
        await self.session.commit()
        await self.cache.invalidate_user(telegram_id)
        return folder

    async def delete_folder(
        self, folder_id: uuid.UUID, telegram_id: int, *, cascade: bool = False
    ) -> int:
        """Delete a folder. Returns the number of files removed with it.

        ``cascade=False`` refuses when the folder still holds content; with
        ``cascade=True`` files are deleted first (freeing quota) and the folder
        tree below it goes away through the ``ON DELETE CASCADE`` FK.
        """
        folder = await self.get_folder(folder_id, telegram_id)
        file_count = int(
            await self.session.scalar(
                select(func.count()).select_from(File).where(File.folder_id == folder_id)
            )
            or 0
        )
        child_count = int(
            await self.session.scalar(
                select(func.count()).select_from(Folder).where(Folder.parent_id == folder_id)
            )
            or 0
        )
        if (file_count or child_count) and not cascade:
            raise FolderNotEmptyError(
                "Folder is not empty", files=file_count, folders=child_count
            )

        removed = 0
        if cascade:
            removed = await self._delete_files_in_subtree(folder_id, telegram_id)
            await self.session.execute(delete(Folder).where(Folder.id == folder_id))
        else:
            await self.session.delete(folder)
        await self.session.commit()
        await self._recalc_and_invalidate(telegram_id)
        return removed

    async def _delete_files_in_subtree(self, folder_id: uuid.UUID, telegram_id: int) -> int:
        """Delete every file inside ``folder_id`` and its descendants."""
        subtree = await self._collect_subtree(folder_id)
        result = await self.session.execute(
            delete(File).where(and_(File.user_id == telegram_id, File.folder_id.in_(subtree)))
        )
        # Share links disappear through the ON DELETE CASCADE FK.
        return int(result.rowcount or 0)

    async def _collect_subtree(self, folder_id: uuid.UUID) -> list[uuid.UUID]:
        """Breadth-first collection of a folder id and all of its descendants."""
        collected: list[uuid.UUID] = [folder_id]
        frontier: list[uuid.UUID] = [folder_id]
        while frontier:
            children = (
                await self.session.scalars(select(Folder.id).where(Folder.parent_id.in_(frontier)))
            ).all()
            new_ids = [child for child in children if child not in collected]
            collected.extend(new_ids)
            frontier = new_ids
        return collected

    async def _recalc_and_invalidate(self, telegram_id: int) -> None:
        """Refresh the denormalised counter and drop cached views."""
        await self.recalculate_storage(telegram_id)
        await self.cache.invalidate_user(telegram_id)

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------
    async def add_file(
        self,
        *,
        telegram_id: int,
        file_id: str,
        file_unique_id: str,
        file_name: str,
        file_size: int,
        mime_type: str | None = None,
        folder_id: uuid.UUID | None = None,
        caption: str | None = None,
        duration: int | None = None,
        width: int | None = None,
        height: int | None = None,
        thumbnail_file_id: str | None = None,
        category: FileCategory | None = None,
        is_public: bool | None = None,
        raise_on_duplicate: bool = False,
        content_path: str | None = None,
    ) -> StoredFile:
        """Persist a Telegram file into the user's cloud.

        Order of checks: de-duplication -> quota -> parent folder -> insert ->
        counter update -> cache invalidation.

        **De-duplication (``hugecloud`` requirement).** ``file_unique_id`` is
        Telegram's content fingerprint: it is identical for the same object no
        matter which chat it was sent from. When a matching row already exists we
        return it immediately (``StoredFile.is_new is False``) instead of paying
        for a second registration - the user sees the file instantly and the
        quota is not charged twice.

        ``raise_on_duplicate=True`` restores the strict behaviour for callers that
        must know about collisions (kept for backwards compatibility).

        ``content_path`` (ixtiyoriy) — yangi yuklangan faylning lokal nusxasi.
        Telegram kengaytmasiz fayl (``tmp_3295``) uchun mime bermasa yoki
        ``application/octet-stream`` qaytarsa, birinchi ~4 KB **kontentdan**
        aniqlanadi va ``mime_type``/``category`` to'g'ri yoziladi.
        """
        safe_name = File.sanitize_filename(file_name)

        # --- kontent bo'yicha tur aniqlash (magic bytes) --------------------
        # Faqat Telegram turi noaniq bo'lganda ishga tushadi: voice/animation
        # kabi aniq klassifikatsiyalar saqlanib qoladi.
        if content_path and (is_generic_mime(mime_type) or category is FileCategory.OTHER):
            detected_mime, detected_category = detect_file(content_path)
            if not is_generic_mime(detected_mime):
                mime_type = detected_mime
                category = detected_category

        if self.settings.storage.dedupe_enabled:
            duplicate = await self.session.scalar(
                select(File)
                .where(
                    and_(File.user_id == telegram_id, File.file_unique_id == file_unique_id)
                )
                .order_by(File.created_at.desc())
            )
            if duplicate is not None:
                # 🗑 Savatdagi nusxa: qayta yuklash = tiklash. UniqueConstraint
                # baribir ikkinchi qatorni taqiqlaydi, foydalanuvchi niyati esa
                # aniq — fayl unga yana kerak.
                if duplicate.deleted_at is not None:
                    restored = await self.restore_file(duplicate.id, telegram_id)
                    if restored.file_id != file_id:
                        restored.file_id = file_id
                        await self.session.commit()
                        await self.cache.delete(self.cache.keys.file(restored.id))
                    return StoredFile(record=restored, is_new=False, deduplicated=True)
                if raise_on_duplicate:
                    raise DuplicateFileError(
                        "This file is already stored in your cloud",
                        file_id=str(duplicate.id),
                        name=duplicate.file_name,
                    )
                logger.info(
                    "Dedupe hit for %s (unique_id=%s) -> %s",
                    safe_name,
                    file_unique_id,
                    duplicate.id,
                )
                # Yangi file_id bilan yangilab qo'yamiz: Telegram ba'zan bir xil
                # obyekt uchun turli chat'da boshqa file_id beradi, eng yangisi
                # ishonchliroq.
                if duplicate.file_id != file_id:
                    duplicate.file_id = file_id
                    await self.session.commit()
                    await self.cache.delete(self.cache.keys.file(duplicate.id))
                return StoredFile(record=duplicate, is_new=False, deduplicated=True)

        user = await self.get_user_or_raise(telegram_id)
        self.ensure_fits(user, file_size)

        if folder_id is not None:
            await self.get_folder(folder_id, telegram_id)

        public = self.settings.storage.default_public if is_public is None else is_public
        record = File(
            user_id=telegram_id,
            folder_id=folder_id,
            file_id=file_id,
            file_unique_id=file_unique_id,
            file_name=safe_name,
            file_size=file_size,
            mime_type=mime_type,
            category=category or File.detect_category(safe_name, mime_type),
            caption=caption,
            duration=duration,
            width=width,
            height=height,
            thumbnail_file_id=thumbnail_file_id,
        )
        record.set_visibility(
            _visibility_from_flag(public)  # type: ignore[arg-type]
        )
        self.session.add(record)

        # Keep the denormalised counter in sync inside the same transaction.
        await self.session.execute(
            update(User)
            .where(User.telegram_id == telegram_id)
            .values(storage_used=User.storage_used + file_size)
        )
        try:
            await self.session.commit()
        except IntegrityError as exc:  # unique (user_id, file_unique_id)
            # Poyga holati: ikkita bir xil fayl parallel keldi. O'chirib
            # tashlamaymiz - mavjud qatorni topib, dedupe natijasi qaytaramiz.
            await self.session.rollback()
            existing = await self.session.scalar(
                select(File).where(
                    and_(File.user_id == telegram_id, File.file_unique_id == file_unique_id)
                )
            )
            if existing is not None and not raise_on_duplicate:
                return StoredFile(record=existing, is_new=False, deduplicated=True)
            raise DuplicateFileError("This file is already stored in your cloud") from exc

        await self.cache.delete(self.cache.keys.user(telegram_id))
        await self.cache.invalidate_user(telegram_id)
        await self.session.refresh(record)
        logger.info("Stored file %s (%s, %d bytes)", record.id, safe_name, file_size)
        return StoredFile(record=record, is_new=True)

    async def get_file(self, file_uuid: uuid.UUID | str, telegram_id: int) -> File:
        """Fetch a file owned by ``telegram_id``."""
        parsed = _to_uuid(file_uuid)
        record = await self.session.scalar(
            select(File)
            .where(and_(File.id == parsed, File.user_id == telegram_id))
            .options(selectinload(File.shared_links))
        )
        if record is None:
            raise FileNotFoundError(f"File {file_uuid} not found")
        return record

    async def get_file_by_key(
        self, key: str, telegram_id: int, *, with_links: bool = True
    ) -> File:
        """Fetch a file by UUID **or** its 32-char hex form, owned by the user.

        The hex form is what the legacy callback grammar carries
        (``pin:<id>``, ``move:<id>`` …) and fits Telegram's 64-byte limit.
        """
        parsed = _parse_uuid_flexible(key)
        statement = select(File).where(and_(File.id == parsed, File.user_id == telegram_id))
        if with_links:
            statement = statement.options(selectinload(File.shared_links))
        record = await self.session.scalar(statement)
        if record is None:
            raise FileNotFoundError(f"File {key} not found")
        return record

    async def get_folder_by_key(self, key: str, telegram_id: int) -> Folder | None:
        """Resolve ``"-"``/``""``/``None`` to root, otherwise load the folder."""
        if not key or key in {"-", "umumiy", "root", "None"}:
            return None
        return await self.get_folder(_parse_uuid_flexible(key), telegram_id)

    async def get_file_any_owner(self, file_uuid: uuid.UUID | str) -> File:
        """Fetch a file regardless of owner (admin/public endpoints only)."""
        record = await self.session.scalar(
            select(File).where(File.id == _to_uuid(file_uuid)).options(selectinload(File.shared_links))
        )
        if record is None:
            raise FileNotFoundError(f"File {file_uuid} not found")
        return record

    def _base_listing_query(self, telegram_id: int) -> Select[tuple[File]]:
        # Savatdagi (soft-deleted) fayllar hech bir oddiy ro'yxatda chiqmaydi —
        # ular faqat maxsus ``list_trash`` orqali ko'rinadi.
        return select(File).where(
            and_(File.user_id == telegram_id, File.deleted_at.is_(None))
        )

    async def list_files(
        self,
        telegram_id: int,
        *,
        folder_id: uuid.UUID | None = _UNSET,
        page: int = 1,
        page_size: int | None = None,
        search: str | None = None,
        category: FileCategory | Sequence[FileCategory] | None = None,
        group: GroupName | str | None = None,
        apps_only: bool = False,
        pinned_only: bool = False,
        sort_by: SortField = "created_at",
        sort_desc: bool = True,
        include_folders: bool = True,
    ) -> tuple[Sequence[File], int]:
        """Paginated listing used by ``/files`` and the bot's storage browser.

        Mirrors the legacy bot's menu tabs:

        * ``category`` accepts one value **or** a sequence (``apps_only=True`` is
          the ``('apk','ipa')`` tab);
        * ``group`` selects one of the four ``/files`` groups - 🎬 ``media``,
          🎵 ``audio``, 📄 ``document``, 📦 ``archive`` (see
          :data:`db.models.FILE_GROUPS`);
        * ``pinned_only`` renders the "📌 Muhimlar" tab;
        * ``folder_id`` defaults to :data:`_UNSET` meaning *no folder filter*
          (the legacy bot listed everything); pass ``None`` explicitly to list
          only root-level files.

        Returns ``(items, total_count)`` so callers can render pagination.
        """
        size = page_size or self.settings.storage.page_size
        page = max(1, page)

        # 🗑 Savatdagi fayllar (deleted_at IS NOT NULL) hech bir ro'yxatda
        # ko'rinmaydi — ular faqat list_trash() orqali olinadi.
        conditions = [File.user_id == telegram_id, File.deleted_at.is_(None)]
        if include_folders and folder_id is not _UNSET:
            conditions.append(File.folder_id == folder_id)
        if search:
            conditions.append(File.file_name.ilike(f"%{search.strip()}%"))
        # 📌 "pinned" — toifa emas, fayl belgisi. Shuning uchun u filtrlar
        # zanjiridan tashqarida qo'llanadi va boshqa filtrlar (apps/category)
        # bilan birga ishlashi mumkin. Qidiruv yo'li (search) ham shunday qiladi;
        # avval bu yerda qayta ishlanmagani uchun ``group=pinned`` barcha fayllarni
        # qaytarar edi.
        if group == "pinned":
            pinned_only = True

        if apps_only or group == "apps":
            conditions.append(File.is_app.is_(True))
        elif group and group not in {"all", "pinned"}:
            group_categories = FILE_GROUPS.get(str(group))
            if group_categories:
                conditions.append(File.category.in_(group_categories))
        elif category is not None:
            if isinstance(category, (list, tuple, set)):
                conditions.append(File.category.in_(tuple(category)))
            else:
                conditions.append(File.category == category)
        if pinned_only:
            conditions.append(File.is_pinned.is_(True))

        where = and_(*conditions) if conditions else True  # type: ignore[assignment]
        statement = (
            select(File)
            .where(where)
            .order_by(
                File.is_pinned.desc(),
                (_SORTABLE_COLUMNS.get(sort_by, File.created_at)).desc()
                if sort_desc
                else (_SORTABLE_COLUMNS.get(sort_by, File.created_at)).asc(),
            )
            .limit(size)
            .offset((page - 1) * size)
        )
        count_stmt = select(func.count()).select_from(File).where(where)

        items = (await self.session.scalars(statement)).unique().all()
        total = int(await self.session.scalar(count_stmt) or 0)
        return items, total

    async def search_files(
        self,
        telegram_id: int,
        query: str,
        *,
        limit: int | None = None,
        group: GroupName | str | None = None,
        pinned_only: bool = False,
    ) -> Sequence[File]:
        """Name search used by Telegram inline mode (cache-friendly).

        ``group`` narrows the search to one ``/files`` group, which is what the
        legacy ``🔍 Qidirish`` flow does when it is opened from a category tab.
        """
        max_items = limit or self.settings.storage.inline_results_limit
        cleaned = query.strip()
        cache_key = self.cache.keys.search(telegram_id, cleaned, max_items)
        if cleaned:
            cached = await self.cache.get(cache_key)
            if cached is not None:
                ids = [uuid.UUID(item) for item in cached]
                rows = (
                    await self.session.scalars(
                        select(File).where(
                            and_(
                                File.user_id == telegram_id,
                                File.deleted_at.is_(None),  # savatdagilar qidiruvdan chiqadi
                                File.id.in_(ids),
                            )
                        )
                    )
                ).unique().all()
                by_id = {row.id: row for row in rows}
                ordered = [by_id[file_id] for file_id in ids if file_id in by_id]
                if ordered:
                    return ordered

        statement = self._base_listing_query(telegram_id)
        if cleaned:
            statement = statement.where(File.file_name.ilike(f"%{cleaned}%"))
        if group == "pinned":
            pinned_only = True
        elif group and group != "all":
            group_categories = FILE_GROUPS.get(str(group))
            if group_categories:
                statement = statement.where(File.category.in_(group_categories))
        if pinned_only:
            statement = statement.where(File.is_pinned.is_(True))
        statement = statement.order_by(File.created_at.desc()).limit(max_items)
        results = (await self.session.scalars(statement)).unique().all()

        if cleaned:
            await self.cache.set(
                cache_key, [str(item.id) for item in results], self.settings.cache.ttl_search
            )
        return results

    async def rename_file(
        self, file_uuid: uuid.UUID | str, telegram_id: int, new_name: str
    ) -> File:
        """Rename a stored file (extension is preserved when omitted)."""
        record = await self.get_file(file_uuid, telegram_id)
        record.rename(new_name)
        await self.session.commit()
        await self.cache.delete(self.cache.keys.file(record.id))
        await self.cache.invalidate_user(telegram_id)
        return record

    async def move_file(
        self, file_uuid: uuid.UUID | str, telegram_id: int, folder_id: uuid.UUID | None
    ) -> File:
        """Move a file into another folder (``None`` == root)."""
        record = await self.get_file(file_uuid, telegram_id)
        if folder_id is not None:
            await self.get_folder(folder_id, telegram_id)
        record.folder_id = folder_id
        await self.session.commit()
        await self.cache.invalidate_user(telegram_id)
        return record

    async def toggle_pin(self, file_uuid: uuid.UUID | str, telegram_id: int) -> File:
        """Flip the "muhim" (pinned) flag - the legacy bot's 📌 feature."""
        record = await self.get_file(file_uuid, telegram_id)
        record.toggle_pin()
        await self.session.commit()
        await self.cache.delete(self.cache.keys.file(record.id))
        await self.cache.invalidate_user(telegram_id)
        return record

    async def list_pinned(
        self, telegram_id: int, *, limit: int | None = None
    ) -> Sequence[File]:
        """Pinned files, newest first (the "📌 Muhimlar" tab)."""
        size = limit or self.settings.storage.page_size
        statement = (
            self._base_listing_query(telegram_id)
            .where(File.is_pinned.is_(True))
            .order_by(File.created_at.desc())
            .limit(size)
        )
        return (await self.session.scalars(statement)).unique().all()

    async def _has_active_link(self, file_uuid: uuid.UUID) -> bool:
        """Faylning ishlaydigan ulashish havolasi bormi?

        Muddat tekshiruvi SQL'da emas, **modelda** (``SharedLink.is_active_now``)
        bajariladi: SQLite ``DateTime(timezone=True)`` ustunini naive qiymat
        sifatida qaytaradi, shuning uchun taqqoslashni o'sha yerdagi mantiqqa
        qoldirish xatosiz va izchil.
        """
        statement = (
            select(SharedLink)
            .where(and_(SharedLink.file_id == file_uuid, SharedLink.is_active.is_(True)))
            .limit(20)
        )
        links = (await self.session.scalars(statement)).all()
        return any(link.is_active_now for link in links)

    async def set_visibility(
        self, file_uuid: uuid.UUID | str, telegram_id: int, is_public: bool
    ) -> File:
        """Toggle the public flag, keeping the enum in sync.

        "Ommaviy"ni o'chirish ikki xil ma'noni ajratadi:

        * fayl ro'yxatlarda ko'rinmasin → ``is_public=False`` (har doam);
        * fayl umuman yopilmasin → faqat **faol havolasi bo'lmasa** ``PRIVATE``.

        Agar egasi allaqachon havola tarqatgan bo'lsa, uni PRIVATE ga tushirish
        o'sha havolalarni jimgina buzadi (``resolve_token`` 410 qaytaradi).
        Shu sababli bunday holda ``UNLISTED`` ga tushiramiz: havola token bilan
        ishlashda davom etadi, fayl esa ommaviy ro'yxatlardan chiqadi.
        """
        from db.models import Visibility

        record = await self.get_file(file_uuid, telegram_id)
        if is_public:
            record.set_visibility(Visibility.PUBLIC)
        else:
            has_live_link = await self._has_active_link(record.id)
            record.set_visibility(Visibility.UNLISTED if has_live_link else Visibility.PRIVATE)
        await self.session.commit()
        await self.cache.delete(self.cache.keys.file(record.id))
        await self.cache.invalidate_user(telegram_id)
        return record

    async def delete_file(self, file_uuid: uuid.UUID | str, telegram_id: int) -> int:
        """🗑 Savatga tashlash (soft delete) — qator o'chirilmaydi.

        ``deleted_at`` to'ldiriladi; ro'yxatlar bu yozuvni ko'rsatmaydi,
        kvota darhol bo'shaydi (foydalanuvchi nuqtai nazaridan fayl "yo'q").
        Qaytarish: :meth:`restore_file`; yakuniy o'chirish: :meth:`purge_file`.
        Takroriy chaqiruv idempotent (savatdagi faylga yana DELETE — xato emas).
        """
        record = await self.get_file(file_uuid, telegram_id)
        size = record.file_size
        already_trashed = record.deleted_at is not None
        if not already_trashed:
            record.deleted_at = datetime.now(UTC)
            await self.session.execute(
                update(User)
                .where(User.telegram_id == telegram_id)
                .values(storage_used=func.max(User.storage_used - size, 0))
            )
        await self.session.commit()
        await self.cache.delete(self.cache.keys.file(record.id))
        await self.cache.invalidate_user(telegram_id)
        logger.info("Trashed file %s (%d bytes) for user %s", record.id, size, telegram_id)
        return 0 if already_trashed else size

    async def restore_file(self, file_uuid: uuid.UUID | str, telegram_id: int) -> File:
        """Savatdan qaytarish: ``deleted_at`` tozalanadi, kvota qayta band bo'ladi."""
        record = await self.get_file(file_uuid, telegram_id)
        if record.deleted_at is None:
            return record  # allaqachon faol — idempotent
        record.deleted_at = None
        await self.session.execute(
            update(User)
            .where(User.telegram_id == telegram_id)
            .values(storage_used=User.storage_used + record.file_size)
        )
        await self.session.commit()
        # server_onupdate ``updated_at``ni expire qiladi; serializer sync
        # kontekstda o'qiganda MissingGreenlet bo'lmasligi uchun yangilaymiz.
        await self.session.refresh(record)
        await self.cache.delete(self.cache.keys.file(record.id))
        await self.cache.invalidate_user(telegram_id)
        logger.info("Restored file %s for user %s", record.id, telegram_id)
        return record

    async def list_trash(
        self, telegram_id: int, *, page: int = 1, page_size: int | None = None
    ) -> tuple[Sequence[File], int]:
        """Savatdagi fayllar (eng so'nggi o'chirilganlar birinchi)."""
        size = page_size or self.settings.storage.page_size
        base = select(File).where(
            and_(File.user_id == telegram_id, File.deleted_at.is_not(None))
        )
        total = int(
            await self.session.scalar(select(func.count()).select_from(base.subquery())) or 0
        )
        rows = (
            await self.session.scalars(
                base.options(selectinload(File.shared_links))
                .order_by(File.deleted_at.desc())
                .offset((max(page, 1) - 1) * size)
                .limit(size)
            )
        ).unique().all()
        return rows, total

    async def purge_file(self, file_uuid: uuid.UUID | str, telegram_id: int) -> int:
        """Savatdagi faylni butunlay o'chirish (qator ham ketadi).

        Kvota allaqachon soft-delete paytida bo'shatilgan, shuning uchun bu
        yerda hisoblagichga tegilmaydi. Faol (savatda bo'lmagan) faylga purge
        chaqirilsa avval savatga tushiriladi — bexosdan darhol yo'qotish yo'q.
        """
        record = await self.get_file(file_uuid, telegram_id)
        if record.deleted_at is None:
            await self.delete_file(file_uuid, telegram_id)
            record = await self.get_file(file_uuid, telegram_id)
        size = record.file_size
        await self.session.delete(record)
        await self.session.commit()
        await self.cache.delete(self.cache.keys.file(record.id))
        await self.cache.invalidate_user(telegram_id)
        logger.info("Purged file %s (%d bytes) for user %s", record.id, size, telegram_id)
        return size

    async def delete_files_bulk(
        self, file_ids: Sequence[uuid.UUID | str], telegram_id: int
    ) -> int:
        """🗑 Ko'p faylni birdan savatga tashlash (soft delete).

        Qatorlar o'chirilmaydi — ``deleted_at`` to'ldiriladi; allaqachon
        savatdagilar qayta hisoblanmaydi (idempotent).
        """
        parsed = [_to_uuid(item) for item in file_ids]
        if not parsed:
            return 0
        result = await self.session.execute(
            update(File)
            .where(
                and_(
                    File.user_id == telegram_id,
                    File.id.in_(parsed),
                    File.deleted_at.is_(None),
                )
            )
            .values(deleted_at=datetime.now(UTC))
        )
        await self.session.commit()
        await self._recalc_and_invalidate(telegram_id)
        return int(result.rowcount or 0)

    async def register_view(self, file_uuid: uuid.UUID | str, *, is_download: bool = False) -> None:
        """Increment view/download counters without loading the whole row."""
        values: dict[str, Any] = {"views": File.views + 1}
        if is_download:
            values["downloads"] = File.downloads + 1
        await self.session.execute(
            update(File).where(File.id == _to_uuid(file_uuid)).values(**values)
        )
        await self.session.commit()
        await self.cache.delete(self.cache.keys.file(_to_uuid(file_uuid)))

    async def stats(self, telegram_id: int) -> dict[str, Any]:
        """Aggregate numbers for the "My storage" screen."""
        cache_key = self.cache.keys.stats(telegram_id)
        cached = await self.cache.get(cache_key)
        if cached:
            return cached

        user = await self.get_user_or_raise(telegram_id)
        # Savatdagi fayllar statistikaga kirmaydi (deleted_at IS NULL).
        _active = and_(File.user_id == telegram_id, File.deleted_at.is_(None))
        total_files = int(
            await self.session.scalar(
                select(func.count()).select_from(File).where(_active)
            )
            or 0
        )
        total_size = int(
            await self.session.scalar(
                select(func.coalesce(func.sum(File.file_size), 0)).where(_active)
            )
            or 0
        )
        by_category_rows = (
            await self.session.execute(
                select(File.category, func.count(), func.coalesce(func.sum(File.file_size), 0))
                .where(_active)
                .group_by(File.category)
            )
        ).all()
        total_folders = int(
            await self.session.scalar(
                select(func.count()).select_from(Folder).where(Folder.user_id == telegram_id)
            )
            or 0
        )
        public_files = int(
            await self.session.scalar(
                select(func.count()).select_from(File).where(
                    and_(File.user_id == telegram_id, File.deleted_at.is_(None), File.is_public.is_(True))
                )
            )
            or 0
        )
        pinned_files = int(
            await self.session.scalar(
                select(func.count()).select_from(File).where(
                    and_(File.user_id == telegram_id, File.deleted_at.is_(None), File.is_pinned.is_(True))
                )
            )
            or 0
        )

        payload: dict[str, Any] = {
            "files": total_files,
            "folders": total_folders,
            "public_files": public_files,
            "pinned_files": pinned_files,
            "used": total_size,
            "quota": self.quota_for(user),
            #: ``0`` kvota = cheksiz; UI buni "Cheksiz (Unlimited)" deb ko'rsatadi.
            "unlimited": user.quota_is_unlimited(self.default_quota),
            "quota_label": self.quota_label(user),
            "free": user.free_space(self.default_quota),
            "percent_used": round((total_size / self.quota_for(user)) * 100, 2)
            if self.quota_for(user)
            else 0.0,
            "by_category": {
                (category.value if isinstance(category, FileCategory) else str(category)): {
                    "count": int(count),
                    "size": int(size),
                }
                for category, count, size in by_category_rows
            },
            "generated_at": datetime.now(UTC).isoformat(),
        }
        await self.cache.set(cache_key, payload, self.settings.cache.ttl_file)
        return payload

    async def cleanup_expired_links(self, *, batch: int = 500) -> int:
        """Deactivate share links whose ``expires_at`` has passed (cron task).

        ``UPDATE ... LIMIT`` is not portable, so expired ids are selected first
        (capped at ``batch``) and then deactivated in one statement.
        """
        now = datetime.now(UTC)
        expired_ids = (
            await self.session.scalars(
                select(SharedLink.id)
                .where(
                    and_(
                        SharedLink.is_active.is_(True),
                        SharedLink.expires_at.is_not(None),
                        SharedLink.expires_at <= now,
                    )
                )
                .limit(batch)
            )
        ).all()
        if not expired_ids:
            return 0

        result = await self.session.execute(
            update(SharedLink)
            .where(SharedLink.id.in_(expired_ids))
            .values(is_active=False)
            .execution_options(synchronize_session=False)
        )
        await self.session.commit()
        logger.info("Deactivated %d expired share links", result.rowcount or 0)
        return int(result.rowcount or 0)

    async def owner_of_file(self, file_uuid: uuid.UUID | str) -> int:
        """Return the ``telegram_id`` owning a file (permission checks)."""
        owner = await self.session.scalar(
            select(File.user_id).where(File.id == _to_uuid(file_uuid))
        )
        if owner is None:
            raise FileNotFoundError(f"File {file_uuid} not found")
        return int(owner)

    async def assert_owner(self, file_uuid: uuid.UUID | str, telegram_id: int) -> None:
        """Raise :class:`NotOwnerError` when the caller is not the owner."""
        owner = await self.owner_of_file(file_uuid)
        if owner != telegram_id and telegram_id not in self.settings.bot.admin_ids:
            raise NotOwnerError("You do not own this file", file_id=str(file_uuid))



    # ------------------------------------------------------------------
    # Admin aggregates (``/diyor`` panel + ``/api/v1/system``)
    # ------------------------------------------------------------------
    async def get_user_by_login(self, login: str) -> User | None:
        """Find an account by its login name (``/login``).

        Case sensitivity follows ``AUTH_CASE_SENSITIVE``; the comparison happens
        in SQL so no full table scan in Python.
        """
        cleaned = (login or "").strip()
        if not cleaned:
            return None
        column = User.login if self.settings.auth.case_sensitive_login else func.lower(User.login)
        needle = cleaned if self.settings.auth.case_sensitive_login else cleaned.lower()
        return await self.session.scalar(select(User).where(column == needle))

    async def is_login_taken(self, login: str, *, exclude_telegram_id: int | None = None) -> bool:
        """``True`` when another account already uses ``login``."""
        cleaned = (login or "").strip()
        if not cleaned:
            return False
        column = User.login if self.settings.auth.case_sensitive_login else func.lower(User.login)
        needle = cleaned if self.settings.auth.case_sensitive_login else cleaned.lower()
        statement = select(User.telegram_id).where(column == needle)
        if exclude_telegram_id is not None:
            statement = statement.where(User.telegram_id != exclude_telegram_id)
        return (await self.session.scalar(statement)) is not None

    async def group_counts(self, telegram_id: int) -> dict[str, int]:
        """``/files`` menyusi uchun guruhlar kesimi (bitta SQL so'rovda).

        Qaytariladigan kalitlar: ``media``, ``audio``, ``document``, ``archive``,
        ``apps``, ``pinned`` va ``all``. Kategoriya -> guruh moslamasi
        :data:`db.models.FILE_GROUPS` orqali amalga oshiriladi; shuning uchun
        yangi kategoriya qo'shilsa bu metod o'zgarishsiz ishlaydi.
        """
        rows = (
            await self.session.execute(
                select(File.category, func.count())
                .where(and_(File.user_id == telegram_id, File.deleted_at.is_(None)))
                .group_by(File.category)
            )
        ).all()

        per_category: dict[str, int] = {}
        for category, count in rows:
            key = category.value if isinstance(category, FileCategory) else str(category)
            per_category[key] = int(count)

        counts: dict[str, int] = {"all": sum(per_category.values())}
        for group, categories in FILE_GROUPS.items():
            counts[group] = sum(
                per_category.get(
                    category.value if isinstance(category, FileCategory) else str(category), 0
                )
                for category in categories
            )

        counts["pinned"] = int(
            await self.session.scalar(
                select(func.count())
                .select_from(File)
                .where(
                    and_(
                        File.user_id == telegram_id,
                        File.deleted_at.is_(None),
                        File.is_pinned.is_(True),
                    )
                )
            )
            or 0
        )
        return counts

    async def admin_stats(self, *, active_window_hours: int = 24) -> dict[str, Any]:
        """📊 Tizim statistikasi: users, sessions, files, indexed size.

        "Active sessions" are accounts seen in the last ``active_window_hours``
        (``users.last_seen_at`` is stamped by the auth middleware), which is the
        closest measurable equivalent of a session counter for a bot.
        """
        since = datetime.now(UTC) - timedelta(hours=max(1, active_window_hours))

        total_users = int(await self.session.scalar(select(func.count()).select_from(User)) or 0)
        blocked_users = int(
            await self.session.scalar(
                select(func.count()).select_from(User).where(User.is_blocked.is_(True))
            )
            or 0
        )
        authenticated_users = int(
            await self.session.scalar(
                select(func.count()).select_from(User).where(User.is_authenticated.is_(True))
            )
            or 0
        )
        with_account = int(
            await self.session.scalar(
                select(func.count()).select_from(User).where(User.login.is_not(None))
            )
            or 0
        )
        active_sessions = int(
            await self.session.scalar(
                select(func.count())
                .select_from(User)
                .where(and_(User.last_seen_at.is_not(None), User.last_seen_at >= since))
            )
            or 0
        )
        premium_users = int(
            await self.session.scalar(
                select(func.count()).select_from(User).where(User.is_premium.is_(True))
            )
            or 0
        )

        total_files = int(await self.session.scalar(select(func.count()).select_from(File)) or 0)
        total_size = int(
            await self.session.scalar(select(func.coalesce(func.sum(File.file_size), 0))) or 0
        )
        total_folders = int(
            await self.session.scalar(select(func.count()).select_from(Folder)) or 0
        )
        public_files = int(
            await self.session.scalar(
                select(func.count()).select_from(File).where(File.is_public.is_(True))
            )
            or 0
        )
        pinned_files = int(
            await self.session.scalar(
                select(func.count()).select_from(File).where(File.is_pinned.is_(True))
            )
            or 0
        )
        archived_files = int(
            await self.session.scalar(
                select(func.count())
                .select_from(File)
                .where(File.archive_message_id.is_not(None))
            )
            or 0
        )
        active_links = int(
            await self.session.scalar(
                select(func.count()).select_from(SharedLink).where(SharedLink.is_active.is_(True))
            )
            or 0
        )

        by_category_rows = (
            await self.session.execute(
                select(File.category, func.count(), func.coalesce(func.sum(File.file_size), 0))
                .group_by(File.category)
                .order_by(func.count().desc())
            )
        ).all()

        top_rows = (
            await self.session.execute(
                select(User, func.count(File.id), func.coalesce(func.sum(File.file_size), 0))
                .join(File, File.user_id == User.telegram_id)
                .group_by(User.telegram_id)
                .order_by(func.coalesce(func.sum(File.file_size), 0).desc())
                .limit(10)
            )
        ).all()

        return {
            "users": {
                "total": total_users,
                "blocked": blocked_users,
                "authenticated": authenticated_users,
                "with_account": with_account,
                "premium": premium_users,
            },
            "active_sessions": active_sessions,
            "active_window_hours": max(1, active_window_hours),
            "files": {
                "total": total_files,
                "public": public_files,
                "pinned": pinned_files,
                "archived": archived_files,
                "pending_archive": max(0, total_files - archived_files),
                "size": total_size,
            },
            "folders": total_folders,
            "shared_links": active_links,
            "by_category": {
                (category.value if isinstance(category, FileCategory) else str(category)): {
                    "count": int(count),
                    "size": int(size),
                }
                for category, count, size in by_category_rows
            },
            "top_users": [
                {
                    "telegram_id": int(row_user.telegram_id),
                    "name": row_user.display_name,
                    "login": row_user.login,
                    "files": int(count),
                    "size": int(size),
                    "blocked": bool(row_user.is_blocked),
                }
                for row_user, count, size in top_rows
            ],
            "quota_label": (
                self.settings.storage.unlimited_label
                if self.settings.storage.default_quota <= 0
                else f"{self.settings.storage.default_quota} bytes"
            ),
            "generated_at": datetime.now(UTC).isoformat(),
        }

    async def list_users(
        self,
        *,
        page: int = 1,
        page_size: int = 10,
        search: str | None = None,
        blocked_only: bool = False,
        authenticated_only: bool = False,
    ) -> tuple[Sequence[User], int]:
        """👥 Paginated user list for the admin panel."""
        page = max(1, page)
        size = max(1, min(page_size, 100))
        conditions = []
        if blocked_only:
            conditions.append(User.is_blocked.is_(True))
        if authenticated_only:
            conditions.append(User.is_authenticated.is_(True))
        if search:
            needle = f"%{search.strip()}%"
            conditions.append(
                User.username.ilike(needle)
                | User.first_name.ilike(needle)
                | User.last_name.ilike(needle)
                | User.login.ilike(needle)
                | User.telegram_id.cast(String).ilike(needle)
            )
        where = and_(*conditions) if conditions else True  # type: ignore[assignment]

        rows = (
            await self.session.scalars(
                select(User)
                .where(where)
                .order_by(User.created_at.desc())
                .limit(size)
                .offset((page - 1) * size)
            )
        ).all()
        total = int(await self.session.scalar(select(func.count()).select_from(User).where(where)) or 0)
        return rows, total

    async def get_user_any(self, telegram_id: int) -> User | None:
        """Admin lookup that does **not** raise (panel shows "topilmadi")."""
        return await self.session.get(User, telegram_id)

    async def set_blocked(
        self, telegram_id: int, blocked: bool, *, note: str | None = None
    ) -> User:
        """Block/unblock an account (admin action, audited by the caller)."""
        user = await self.get_user_or_raise(telegram_id)
        if blocked:
            user.block(note)
        else:
            user.unblock()
        await self.session.commit()
        await self.cache.invalidate_user(telegram_id)
        logger.info("User %s blocked=%s", telegram_id, blocked)
        return user

    async def reset_password(
        self, telegram_id: int, new_password: str | None = None
    ) -> tuple[User, str]:
        """Reset an account password; returns ``(user, plain_password)``.

        When ``new_password`` is omitted a random one is generated - the admin
        sees it exactly once in the ``/diyor`` panel and forwards it manually.
        """
        import secrets

        user = await self.get_user_or_raise(telegram_id)
        password = new_password or secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
        user.set_password(password)
        user.reset_failed_logins()
        await self.session.commit()
        await self.cache.invalidate_user(telegram_id)
        return user, password

    async def set_user_quota(self, telegram_id: int, quota: int | None) -> User:
        """Per-user quota override (``None``/``0`` = unlimited)."""
        user = await self.get_user_or_raise(telegram_id)
        user.storage_quota = None if not quota or quota <= 0 else int(quota)
        await self.session.commit()
        await self.cache.invalidate_user(telegram_id)
        return user

    async def broadcast_targets(
        self, *, include_blocked: bool = False, only_authenticated: bool = False
    ) -> list[int]:
        """📢 Recipient ids for a broadcast (blocked accounts excluded)."""
        conditions = []
        if not include_blocked:
            conditions.append(User.is_blocked.is_(False))
        if only_authenticated:
            conditions.append(User.is_authenticated.is_(True))
        statement = select(User.telegram_id)
        for condition in conditions:
            statement = statement.where(condition)
        return [int(row) for row in (await self.session.scalars(statement)).all()]

    async def user_files_count(self, telegram_id: int) -> tuple[int, int]:
        """``(files, bytes)`` of one user - shown in the admin user card."""
        _active = and_(File.user_id == telegram_id, File.deleted_at.is_(None))
        count = int(
            await self.session.scalar(
                select(func.count()).select_from(File).where(_active)
            )
            or 0
        )
        size = int(
            await self.session.scalar(
                select(func.coalesce(func.sum(File.file_size), 0)).where(_active)
            )
            or 0
        )
        return count, size

    async def recent_audit(
        self, *, limit: int = 15, action: str | None = None, only_failed: bool = False
    ) -> tuple[Sequence[AuditLog], int]:
        """Audit trail slice for the admin panel."""
        conditions = []
        if action:
            conditions.append(AuditLog.action == action)
        if only_failed:
            conditions.append(AuditLog.success.is_(False))
        statement = select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        count_statement = select(func.count()).select_from(AuditLog)
        for condition in conditions:
            statement = statement.where(condition)
            count_statement = count_statement.where(condition)
        rows = (await self.session.scalars(statement.limit(max(1, limit)))).all()
        total = int(await self.session.scalar(count_statement) or 0)
        return rows, total

def _visibility_from_flag(is_public: bool):
    """Map the boolean API flag onto the :class:`Visibility` enum."""
    from db.models import Visibility

    return Visibility.PUBLIC if is_public else Visibility.PRIVATE


def _parse_uuid_flexible(value: str | uuid.UUID) -> uuid.UUID:
    """Accept canonical UUIDs, 32-char hex strings and ``urn:uuid:`` forms."""
    if isinstance(value, uuid.UUID):
        return value
    text = str(value).strip()
    text = text.removeprefix("urn:uuid:")
    if len(text) == 32 and all(char in "0123456789abcdefABCDEF" for char in text):
        return uuid.UUID(text)
    return uuid.UUID(text)


def _to_uuid(value: uuid.UUID | str) -> uuid.UUID:
    """Coerce a path parameter into a :class:`uuid.UUID`."""
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise FileNotFoundError(f"Malformed file id: {value!r}") from exc
