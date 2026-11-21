"""``Users`` table - one row per Telegram account using the cloud."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, TypedDict

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.database import Base
from db.models.base import TimestampMixin
from db.models.password import (
    DEFAULT_ITERATIONS,
    hash_password,
    needs_rehash,
    verify_password,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids circular imports
    from db.models.file import File
    from db.models.folder import Folder


class UserSettings(TypedDict, total=False):
    """Shape of the ``users.settings`` JSON column.

    Stored as JSON so product tweaks never require a migration.
    """

    language: str            # ISO-639-1 code reported by Telegram
    theme: str               # "dark" | "light" | "auto" (Mini App)
    view_mode: str           # "list" | "grid" (Mini App)
    sort_by: str             # "created_at" | "name" | "size"
    sort_desc: bool
    #: ``/files`` saralash kaliti: new | old | big | small | az | za
    #: (see :data:`bot.keyboards.hugecloud.SORTS`).
    sort: str
    public_by_default: bool  # new uploads become public automatically
    inline_enabled: bool     # expose files to @bot inline search
    notify_uploads: bool


#: Defaults applied when a user is created (mirrors ``UserSettings``).
DEFAULT_USER_SETTINGS: UserSettings = {
    "theme": "auto",
    "view_mode": "list",
    "sort_by": "created_at",
    "sort_desc": True,
    "sort": "new",
    "public_by_default": False,
    "inline_enabled": True,
    "notify_uploads": True,
}


class User(Base, TimestampMixin):
    """A Telegram user with a personal storage quota.

    ``telegram_id`` doubles as the primary key because Telegram IDs are unique,
    stable and fit in a ``BigInteger`` - no surrogate key needed.
    """

    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_username", "username"),
        Index("ix_users_is_blocked", "is_blocked"),
        # Login lookups happen on every /login and every guarded update.
        Index("ix_users_login", "login"),
        Index("ix_users_is_authenticated", "is_authenticated"),
    )

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    language_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    is_premium: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Set when the user blocked the bot - stops pointless send attempts.
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Password-gate (legacy ``authorized_users`` set) - persisted so a restart
    #: or a redeploy does not ask everybody for the password again.
    is_authorized: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # --- account credentials (``/register`` + ``/login``) ---------------------
    #: Unique account name chosen by the user. Independent from the Telegram
    #: ``username`` (which people change or never set).
    login: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    #: ``pbkdf2_sha256$iterations$salt$hash`` - never a plain password.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: ``True`` once the user authenticated with :mod:`bot.handlers.auth`.
    #: The auth-guard middleware refuses cloud features while this is false.
    is_authenticated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Consecutive wrong passwords (reset on success).
    failed_login_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Temporary lockout deadline; ``None`` when the account is not locked.
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: ``/diyor`` shows this as the "active sessions" heartbeat.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Human-readable note an admin can attach (reason of a block, etc.).
    admin_note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- storage accounting --------------------------------------------------
    #: Denormalised total size of all files (kept in sync by ``StorageService``).
    storage_used: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    #: Per-user quota override; ``None`` falls back to the global default.
    storage_quota: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    #: Free-form preferences, see :class:`UserSettings`.
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=lambda: dict(DEFAULT_USER_SETTINGS), nullable=False
    )

    # --- relationships -------------------------------------------------------
    # All collections are ``noload``: implicit lazy IO would raise
    # ``MissingGreenlet`` inside the async loop. Services request what they need
    # with ``selectinload(...)`` / ``joinedload(...)``.
    folders: Mapped[list[Folder]] = relationship(
        "Folder",
        back_populates="owner",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="noload",
    )
    files: Mapped[list[File]] = relationship(
        "File",
        back_populates="owner",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="noload",
    )

    # --- behaviour -----------------------------------------------------------
    @property
    def display_name(self) -> str:
        """Best available human readable name (never empty)."""
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        if self.first_name:
            return self.first_name
        if self.username:
            return f"@{self.username}"
        return f"ID {self.telegram_id}"

    @property
    def mention_html(self) -> str:
        """HTML mention safe for ``parse_mode=HTML`` messages."""
        return (
            f'<a href="tg://user?id={self.telegram_id}">'
            f"{self.first_name or self.username or self.telegram_id}</a>"
        )

    # --- credentials ---------------------------------------------------------
    @property
    def has_password(self) -> bool:
        """``True`` when an account password is set (``/register`` completed)."""
        return bool(self.password_hash)

    @property
    def is_locked(self) -> bool:
        """``True`` while a temporary lockout is in effect."""
        if self.locked_until is None:
            return False
        deadline = self.locked_until
        if deadline.tzinfo is None:  # SQLite returns naive datetimes
            deadline = deadline.replace(tzinfo=UTC)
        return datetime.now(UTC) < deadline

    @property
    def lock_seconds_left(self) -> int:
        """Remaining lockout seconds (``0`` when not locked)."""
        if self.locked_until is None:
            return 0
        deadline = self.locked_until
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        return max(0, int((deadline - datetime.now(UTC)).total_seconds()))

    def set_password(self, password: str, *, iterations: int = DEFAULT_ITERATIONS) -> None:
        """Hash and store a new password (salt is generated inside)."""
        self.password_hash = hash_password(password, iterations=iterations)

    def check_password(self, password: str) -> bool:
        """Constant-time verification of ``password``."""
        return verify_password(password, self.password_hash)

    def password_is_outdated(self) -> bool:
        """``True`` when the stored hash predates the current iteration count."""
        return needs_rehash(self.password_hash)

    def register_credentials(self, login: str, password: str) -> None:
        """Attach an account (``/register``): login + hash + authenticated flag."""
        self.login = login.strip()[:64]
        self.set_password(password)
        self.is_authenticated = True
        self.failed_login_attempts = 0
        self.locked_until = None
        self.last_login_at = datetime.now(UTC)

    def mark_authenticated(self) -> None:
        """Successful ``/login``: clear counters, stamp the session."""
        self.is_authenticated = True
        self.failed_login_attempts = 0
        self.locked_until = None
        self.last_login_at = datetime.now(UTC)
        self.last_seen_at = self.last_login_at

    def register_failed_login(self, *, lock_seconds: int = 0) -> int:
        """Count a wrong password; optionally lock the account.

        Returns the new attempt counter so handlers can warn the user
        ("2/5 urinish qoldi").
        """
        self.failed_login_attempts = int(self.failed_login_attempts or 0) + 1
        if lock_seconds > 0:
            self.locked_until = datetime.now(UTC) + timedelta(seconds=lock_seconds)
            self.is_authenticated = False
        return self.failed_login_attempts

    def reset_failed_logins(self) -> None:
        """Clear the counter without touching ``is_authenticated``."""
        self.failed_login_attempts = 0
        self.locked_until = None

    def block(self, note: str | None = None) -> None:
        """Admin block (``/diyor`` -> 👥 Foydalanuvchilar)."""
        self.is_blocked = True
        self.is_authenticated = False
        if note:
            self.admin_note = note[:255]

    def unblock(self) -> None:
        """Admin unblock."""
        self.is_blocked = False
        self.admin_note = None

    def logout(self) -> None:
        """``/logout`` - forget the session but keep the credentials."""
        self.is_authenticated = False

    # --- storage accounting --------------------------------------------------
    def quota(self, default: int) -> int:
        """Effective quota in bytes: per-user override, else the global default.

        ``0`` (or ``None``) means **unlimited** - see :meth:`free_space`.
        """
        return self.storage_quota if self.storage_quota is not None else default

    def free_space(self, default_quota: int) -> int:
        """Remaining bytes (never negative). ``0`` quota means unlimited."""
        limit = self.quota(default_quota)
        if limit <= 0:
            return 1 << 62  # effectively unlimited
        return max(0, limit - self.storage_used)

    def quota_is_unlimited(self, default_quota: int) -> bool:
        """``True`` when this account has no ceiling (rendered as "Cheksiz")."""
        return self.quota(default_quota) <= 0

    def has_space(self, size: int, default_quota: int) -> bool:
        """``True`` when ``size`` more bytes still fit into the quota."""
        return size <= self.free_space(default_quota)

    def setting(self, key: str, fallback: Any = None) -> Any:
        """Read one preference with a safe fallback."""
        value = (self.settings or {}).get(key, fallback)
        if value is None:
            return DEFAULT_USER_SETTINGS.get(key, fallback)  # type: ignore[return-value]
        return value

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<User telegram_id={self.telegram_id} login={self.login!r} "
            f"username={self.username!r} auth={self.is_authenticated} "
            f"blocked={self.is_blocked} used={self.storage_used}>"
        )
