"""``SharedLinks`` table - public tokens that expose a file outside Telegram."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.database import Base
from db.models.base import TimestampMixin

if TYPE_CHECKING:  # pragma: no cover
    from db.models.file import File

#: URL-safe alphabet used for tokens (no ambiguous characters removed on purpose:
#: tokens are never typed by hand, only shared as links).
_TOKEN_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"


def generate_token(nbytes: int = 24) -> str:
    """Return a cryptographically strong, URL-safe share token."""
    return secrets.token_urlsafe(nbytes)


def generate_pass_code(length: int = 6) -> str:
    """Return a human-typeable numeric access code (default 6 digits)."""
    length = max(4, min(12, length))
    return "".join(secrets.choice("0123456789") for _ in range(length))


class SharedLink(Base, TimestampMixin):
    """One shareable link for one file.

    A file may have many links (different expiry / pass codes). Revoking is a
    soft delete (``is_active=False``) so statistics survive.
    """

    __tablename__ = "shared_links"
    __table_args__ = (
        UniqueConstraint("token", name="uq_shared_links_token"),
        Index("ix_shared_links_file", "file_id"),
        Index("ix_shared_links_expires", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    #: FK to ``files.id`` (named ``file_id`` to match the schema spec).
    file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), nullable=False
    )
    #: Public token used in ``t.me/<bot>?start=<token>`` and ``/s/<token>``.
    token: Mapped[str] = mapped_column(String(64), default=lambda: generate_token(), nullable=False)
    #: ``NULL`` == never expires.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Salted SHA-256 of the optional access code (never store it in clear).
    pass_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # --- usage statistics ----------------------------------------------------
    views: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    downloads: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: ``telegram_id`` of the user who created the link (denormalised for audit).
    created_by: Mapped[int | None] = mapped_column(nullable=True)

    # --- relationships -------------------------------------------------------
    file: Mapped[File] = relationship("File", back_populates="shared_links", lazy="joined")

    # --- behaviour -----------------------------------------------------------
    @property
    def is_expired(self) -> bool:
        """``True`` once ``expires_at`` is in the past."""
        if self.expires_at is None:
            return False
        expires = self.expires_at
        if expires.tzinfo is None:            # SQLite returns naive datetimes
            expires = expires.replace(tzinfo=UTC)
        return expires <= datetime.now(UTC)

    @property
    def is_active_now(self) -> bool:
        """Link usable right now: not revoked and not expired."""
        return bool(self.is_active) and not self.is_expired

    @property
    def is_valid(self) -> bool:
        """Alias of :attr:`is_active_now` for readability in handlers."""
        return self.is_active_now

    @property
    def has_pass_code(self) -> bool:
        """``True`` when the link is protected by an access code."""
        return bool(self.pass_code)

    @property
    def seconds_left(self) -> int | None:
        """Remaining lifetime in seconds (``None`` = unlimited)."""
        if self.expires_at is None:
            return None
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return max(0, int((expires - datetime.now(UTC)).total_seconds()))

    @classmethod
    def create_for(
        cls,
        file_id: uuid.UUID,
        *,
        ttl_seconds: int | None = None,
        pass_code: str | None = None,
        created_by: int | None = None,
        token: str | None = None,
    ) -> SharedLink:
        """Factory applying product defaults (expiry + hashed pass code)."""
        expires_at: datetime | None = None
        if ttl_seconds and ttl_seconds > 0:
            expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)

        return cls(
            file_id=file_id,
            token=token or generate_token(),
            expires_at=expires_at,
            pass_code=hash_pass_code(pass_code) if pass_code else None,
            created_by=created_by,
        )

    def extend(self, ttl_seconds: int) -> datetime:
        """Push ``expires_at`` forward from *now* and re-activate the link."""
        self.expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        self.is_active = True
        return self.expires_at

    def revoke(self) -> None:
        """Soft-delete the link."""
        self.is_active = False

    def register_access(self, *, is_download: bool = False) -> None:
        """Bump counters after a successful fetch/stream."""
        self.views += 1
        if is_download:
            self.downloads += 1
        self.last_accessed_at = datetime.now(UTC)

    def to_dict(self) -> dict[str, object]:
        """JSON payload for the REST API (never exposes the pass code hash)."""
        return {
            "id": str(self.id),
            "file_id": str(self.file_id),
            "token": self.token,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "seconds_left": self.seconds_left,
            "is_active": self.is_active_now,
            "has_pass_code": self.has_pass_code,
            "views": self.views,
            "downloads": self.downloads,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SharedLink token={self.token!r} file_id={self.file_id} active={self.is_active}>"


# ---------------------------------------------------------------------------
# Pass-code hashing. Implemented locally to keep the model import-cheap; the API
# layer re-exports the same helpers.
# ---------------------------------------------------------------------------
def hash_pass_code(raw: str, *, salt: str | None = None) -> str:
    """Return ``salt$sha256(salt+code)`` for an access code."""
    import hashlib

    salt = salt or secrets.token_hex(8)
    digest = hashlib.sha256(f"{salt}:{raw}".encode()).hexdigest()
    return f"{salt}${digest}"


def verify_pass_code(raw: str, stored: str | None) -> bool:
    """Constant-time verification of a submitted access code."""
    import hashlib
    import hmac

    if not stored or not raw:
        return False
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    candidate = hashlib.sha256(f"{salt}:{raw}".encode()).hexdigest()
    return hmac.compare_digest(candidate, digest)
