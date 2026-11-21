"""``system_flags`` — ish vaqtida o'zgaradigan tizim bayroqlari.

``/diyor`` -> ⚙️ Tizim sozlamalari ba'zi sozlamalarni **qayta ishga tushirmasdan**
o'zgartirish imkonini beradi (masalan, autentifikatsiyani yoqish/o'chirish).
Pydantic sozlamalari ``.env`` dan o'qiladi va jarayon davomida o'zgarmas
bo'lgani uchun, bunday "jonli" qiymatlar kichik key/value jadvalda saqlanadi va
startup'da qayta o'qiladi.

Qoida: ``.env`` — sukut qiymat, ``system_flags`` — operatorning oxirgi qarori.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, Text, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from db.database import Base
from db.models.base import utcnow

__all__ = ["FLAG_ALLOW_REGISTER", "FLAG_AUTH_REQUIRED", "SystemFlag", "get_flag", "set_flag"]

#: Autentifikatsiya talab qilinsinmi (``AUTH_REQUIRED`` ning jonli ko'rinishi).
FLAG_AUTH_REQUIRED = "auth_required"
#: ``/register`` ochiqmi (``AUTH_ALLOW_REGISTER``).
FLAG_ALLOW_REGISTER = "auth_allow_register"


class SystemFlag(Base):
    """Bitta ``key`` -> ``value`` juftligi (qiymat matn ko'rinishida)."""

    __tablename__ = "system_flags"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    #: Kim o'zgartirgan (Telegram id) — audit uchun.
    updated_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        default=utcnow,
        nullable=False,
    )

    def to_bool(self, default: bool = False) -> bool:
        """Matnli qiymatni ``bool`` ga aylantirish."""
        return _parse_bool(self.value, default)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SystemFlag {self.key}={self.value!r}>"


def _parse_bool(value: Any, default: bool = False) -> bool:
    """``"1"/"true"/"on"/"yes"`` -> ``True`` (qolganlari ``default``)."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "on", "yes", "y", "ha"}


async def get_flag(session: AsyncSession, key: str, default: str | None = None) -> str | None:
    """Bayroq qiymatini o'qish (yo'q bo'lsa ``default``)."""
    row = await session.scalar(select(SystemFlag).where(SystemFlag.key == key))
    return row.value if row is not None else default


async def set_flag(
    session: AsyncSession,
    key: str,
    value: Any,
    *,
    updated_by: int | None = None,
    commit: bool = True,
) -> SystemFlag:
    """Bayroqni yozish (upsert)."""
    row = await session.scalar(select(SystemFlag).where(SystemFlag.key == key))
    text_value = "1" if value is True else "0" if value is False else str(value)
    if row is None:
        row = SystemFlag(key=key, value=text_value, updated_by=updated_by)
        session.add(row)
    else:
        row.value = text_value
        row.updated_by = updated_by
    if commit:
        await session.commit()
    return row


async def load_flags(session: AsyncSession) -> dict[str, str]:
    """Barcha bayroqlar (startup'da sozlamalarga qo'llaniladi)."""
    rows = (await session.scalars(select(SystemFlag))).all()
    return {row.key: row.value for row in rows}
