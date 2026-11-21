"""Audit xizmati — ``audit_logs`` jadvali bilan ishlash.

Maqsad: xavfsizlik uchun muhim hodisalarni (login/register, muvaffaqiyatsiz
parol urinishlari, admin bloklash, parol tiklash, broadcast, arxiv xatolari)
**bitta joyda**, bir xil formatda yozish. ``/diyor`` paneli shu jadval orqali
"nima bo'ldi?" degan savolga javob beradi.

Qoidalar:
* audit yozuvi hech qachon asosiy operatsiyani buzmaydi — xatolar ``logger`` ga
  tushadi va yutiladi (``safe_log``);
* ``commit=False`` bilan chaqirilsa yozuv mavjud tranzaksiyaga qo'shiladi
  (masalan, foydalanuvchini bloklash + audit bitta commit'da);
* so'rovlar sahifalanadi (``limit``/``offset``), chunki jadval cheksiz o'sadi.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import AuditAction, AuditLog

logger = logging.getLogger(__name__)

__all__ = ["AuditService"]


class AuditService:
    """``audit_logs`` ga yozish va o'qish."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Yozish
    # ------------------------------------------------------------------
    async def log(
        self,
        action: str,
        *,
        actor_id: int | None = None,
        target_id: int | None = None,
        detail: str | None = None,
        extra: dict[str, Any] | None = None,
        success: bool = True,
        source: str = "bot",
        ip_address: str | None = None,
        commit: bool = False,
    ) -> AuditLog:
        """Bitta audit yozuvini qo'shish.

        ``commit=True`` bo'lganda darhol commit qilinadi (fon vazifalari va
        mustaqil hodisalar uchun); aks holda chaqiruvchi tranzaksiyasi bilan
        birga saqlanadi.
        """
        entry = AuditLog.create(
            action,
            actor_id=actor_id,
            target_id=target_id,
            detail=(detail[:2000] if detail else None),
            extra=extra,
            success=success,
            source=source,
            ip_address=ip_address,
        )
        self.session.add(entry)
        if commit:
            await self.session.commit()
        else:
            # Tranzaksiya ichida flush qilish id ni olish uchun yetarli.
            await self.session.flush()
        return entry

    async def safe_log(
        self,
        action: str,
        *,
        actor_id: int | None = None,
        target_id: int | None = None,
        detail: str | None = None,
        extra: dict[str, Any] | None = None,
        success: bool = True,
        source: str = "bot",
        ip_address: str | None = None,
        commit: bool = True,
    ) -> None:
        """Xatoga chidamli variant: audit yozilmasa ham ish davom etadi."""
        try:
            await self.log(
                action,
                actor_id=actor_id,
                target_id=target_id,
                detail=detail,
                extra=extra,
                success=success,
                source=source,
                ip_address=ip_address,
                commit=commit,
            )
        except Exception:
            logger.debug("Audit write failed (%s)", action, exc_info=True)
            try:
                await self.session.rollback()
            except Exception:  # rollback muvaffaqiyatsiz bo'lsa ham audit yozuvi yiqilmasin
                logger.debug("Audit rollback failed", exc_info=True)

    # ------------------------------------------------------------------
    # O'qish (admin panel)
    # ------------------------------------------------------------------
    async def recent(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        actor_id: int | None = None,
        target_id: int | None = None,
        action: str | None = None,
        only_failed: bool = False,
    ) -> tuple[Sequence[AuditLog], int]:
        """So'nggi yozuvlar + umumiy son (sahifalash uchun)."""
        conditions = []
        if actor_id is not None:
            conditions.append(AuditLog.actor_id == actor_id)
        if target_id is not None:
            conditions.append(AuditLog.target_id == target_id)
        if action:
            conditions.append(AuditLog.action == action)
        if only_failed:
            conditions.append(AuditLog.success.is_(False))

        statement = select(AuditLog).order_by(desc(AuditLog.created_at), desc(AuditLog.id))
        count_statement = select(func.count()).select_from(AuditLog)
        for condition in conditions:
            statement = statement.where(condition)
            count_statement = count_statement.where(condition)

        rows = (
            await self.session.scalars(statement.limit(max(1, limit)).offset(max(0, offset)))
        ).all()
        total = int(await self.session.scalar(count_statement) or 0)
        return rows, total

    async def count(self, action: str | None = None, *, success: bool | None = None) -> int:
        """Biror turdagi yozuvlar soni (statistika uchun)."""
        statement = select(func.count()).select_from(AuditLog)
        if action:
            statement = statement.where(AuditLog.action == action)
        if success is not None:
            statement = statement.where(AuditLog.success.is_(success))
        return int(await self.session.scalar(statement) or 0)

    async def failed_logins(self, *, limit: int = 20) -> Sequence[AuditLog]:
        """So'nggi muvaffaqiyatsiz login urinishlari (xavfsizlik ko'rinishi)."""
        rows, _ = await self.recent(action=AuditAction.LOGIN_FAIL, limit=limit, only_failed=True)
        return rows

    async def prune(self, *, older_than_days: int = 90) -> int:
        """Eski yozuvlarni tozalash (ixtiyoriy, ``/diyor`` orqali chaqiriladi)."""
        from datetime import datetime, timedelta

        from sqlalchemy import delete

        cutoff = datetime.now(UTC) - timedelta(days=max(1, older_than_days))
        result = await self.session.execute(
            delete(AuditLog).where(AuditLog.created_at < cutoff)
        )
        await self.session.commit()
        return int(result.rowcount or 0)
