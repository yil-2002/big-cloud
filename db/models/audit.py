"""``audit_logs`` - who did what, when.

An append-only trail of security-relevant actions: logins, registrations, failed
password attempts, admin blocks/unblocks, password resets, broadcasts and
deletions. It exists so ``/diyor`` can answer "why is this user locked out?" and
so an operator can reconstruct an incident without reading server logs.

Rows are cheap (one insert, no updates) and never cascade-deleted with a user:
the history must outlive the account. ``actor_id``/``target_id`` therefore stay
plain ``BigInteger`` columns instead of foreign keys.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.database import Base
from db.models.base import utcnow

__all__ = ["AuditAction", "AuditLog"]


class AuditAction:
    """Well-known action names (kept as strings so new ones need no migration)."""

    REGISTER: str = "register"
    LOGIN_OK: str = "login_ok"
    LOGIN_FAIL: str = "login_fail"
    LOGOUT: str = "logout"
    ACCOUNT_LOCKED: str = "account_locked"
    PASSWORD_RESET: str = "password_reset"
    PASSWORD_CHANGE: str = "password_change"
    ADMIN_BLOCK: str = "admin_block"
    ADMIN_UNBLOCK: str = "admin_unblock"
    ADMIN_AUTH_TOGGLE: str = "admin_auth_toggle"
    BROADCAST: str = "broadcast"
    FILE_UPLOAD: str = "file_upload"
    FILE_DELETE: str = "file_delete"
    FILE_ARCHIVED: str = "file_archived"
    ARCHIVE_FAILED: str = "archive_failed"
    SHARE_CREATE: str = "share_create"
    SHARE_REVOKE: str = "share_revoke"


class AuditLog(Base):
    """One immutable record of an action."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_actor", "actor_id"),
        Index("ix_audit_target", "target_id"),
        Index("ix_audit_action_created", "action", "created_at"),
    )

    #: Surrogate key. ``BigInteger`` Postgres'da ``BIGSERIAL`` bo'ladi, lekin
    #: SQLite'da faqat ``INTEGER PRIMARY KEY`` avtomatik to'ladi — shuning uchun
    #: ``.with_variant`` orqali dialektga mos tip tanlanadi (aks holda
    #: ``NOT NULL constraint failed: audit_logs.id``).
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )

    #: Who acted (Telegram id). ``None`` for anonymous/system events.
    actor_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    #: Who was acted upon (e.g. the blocked user).
    target_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    #: One of :class:`AuditAction` (free-form string on purpose).
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: Human readable summary shown in the admin panel.
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Structured payload (ids, counters) - JSON keeps it schema-free.
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    #: ``False`` marks a rejected/failed attempt (login failures, archive errors).
    success: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Source: ``bot``, ``api``, ``system``.
    source: Mapped[str] = mapped_column(String(16), default="bot", nullable=False)
    #: Client IP when known (API requests, webhook updates).
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utcnow, nullable=False
    )

    @classmethod
    def create(
        cls,
        action: str,
        *,
        actor_id: int | None = None,
        target_id: int | None = None,
        detail: str | None = None,
        extra: dict[str, Any] | None = None,
        success: bool = True,
        source: str = "bot",
        ip_address: str | None = None,
    ) -> AuditLog:
        """Build (not persist) an audit row - the caller adds it to the session."""
        return cls(
            action=action,
            actor_id=actor_id,
            target_id=target_id,
            detail=detail,
            extra=extra,
            success=success,
            source=source,
            ip_address=ip_address,
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON representation for the admin panel / REST API."""
        return {
            "id": self.id,
            "actor_id": self.actor_id,
            "target_id": self.target_id,
            "action": self.action,
            "detail": self.detail,
            "extra": self.extra,
            "success": self.success,
            "source": self.source,
            "ip_address": self.ip_address,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<AuditLog {self.action} actor={self.actor_id} ok={self.success}>"
