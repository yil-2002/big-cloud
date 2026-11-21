"""Reusable declarative mixins (timestamps, string repr)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

__all__ = ["TimestampMixin", "utcnow"]


def utcnow() -> datetime:
    """Timezone-aware ``now()`` in UTC - used for Python-side defaults."""
    return datetime.now(UTC)


class TimestampMixin:
    """Adds ``created_at`` / ``updated_at`` maintained by the database."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=utcnow,
        nullable=False,
        sort_order=-2,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        default=utcnow,
        nullable=False,
        sort_order=-1,
    )
