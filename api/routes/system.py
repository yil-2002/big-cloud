"""``/system`` — health, version, global statistics and admin operations.

Render (va boshqa PaaS) health-check'i ``GET /`` ni so'raydi — bu marshrut
:mod:`api.app` da (ildizda) joylashgan va ``"OK"`` qaytaradi. Bu yerda esa
**prefiks ostidagi** kengaytirilgan variantlar: JSON holat, versiya, tizim
statistikasi, audit va jonli sozlamalar.

Admin marshrutlari :func:`api.dependencies.require_admin` orqali himoyalangan
(``ADMIN_ID`` / ``ADMIN_IDS``).
"""

from __future__ import annotations

import logging
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_db, get_settings_dep, require_admin
from api.schemas import MessageResponse
from config import Settings
from db.models import AuditLog, File, Folder, SharedLink, User
from db.models.flag import FLAG_ALLOW_REGISTER, FLAG_AUTH_REQUIRED, set_flag
from services.cache import get_cache
from services.storage import StorageService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/system", tags=["system"])

#: Jarayon ishga tushgan vaqt (uptime hisoblash uchun).
_STARTED_AT = time.time()


def mark_started() -> None:
    """Startup'da uptime hisoblagichini yangilash (:mod:`api.app` chaqiradi)."""
    global _STARTED_AT
    _STARTED_AT = time.time()


@router.get("/health", summary="JSON health report")
async def health(settings: Annotated[Settings, Depends(get_settings_dep)]) -> dict[str, Any]:
    """Dependency health: DB is always checked, Redis degrades gracefully."""
    from db.database import get_session_factory

    database_ok = False
    try:
        factory = get_session_factory()
        async with factory() as session:
            await session.execute(select(func.count()).select_from(User))
        database_ok = True
    except Exception as exc:
        logger.warning("Health check: database unavailable: %s", exc)

    cache = get_cache()
    cache_ok = await cache.ping()

    return {
        "status": "ok" if database_ok else "degraded",
        "version": settings.app.version,
        "env": settings.app.env,
        "database": "up" if database_ok else "down",
        "cache": "up" if cache_ok else "down",
        "uptime_seconds": round(time.time() - _STARTED_AT, 1),
    }


@router.get("/version", summary="Build information")
async def version(settings: Annotated[Settings, Depends(get_settings_dep)]) -> dict[str, Any]:
    """Version, runtime and feature flags (no secrets)."""
    import platform
    import sys

    return {
        "name": settings.app.name,
        "version": settings.app.version,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "language": settings.app.language,
        "features": {
            "auth_required": settings.auth.required,
            "registration_open": settings.auth.allow_registration,
            "archive_channel": settings.archive.channel_id or None,
            "archive_enabled": settings.archive.configured,
            "stream_proxy": settings.storage.stream_proxy_enabled,
            "browse_send_media": settings.storage.browse_send_media,
            "page_size": settings.storage.page_size,
            "quota": settings.storage.default_quota,
            "quota_label": (
                settings.storage.unlimited_label
                if settings.storage.default_quota <= 0
                else f"{settings.storage.default_quota} bytes"
            ),
        },
    }


@router.get("/stats", summary="📊 Global system statistics (admin)")
async def system_stats(
    admin: Annotated[User, Depends(require_admin)],
    storage: Annotated[StorageService, Depends(_storage_dep)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    active_window_hours: Annotated[int, Query(ge=1, le=24 * 30)] = 24,
) -> dict[str, Any]:
    """The ``/diyor`` 📊 panel as JSON (users, sessions, files, size)."""
    del admin, settings
    return await storage.admin_stats(active_window_hours=active_window_hours)


@router.get("/audit", summary="📜 Audit log (admin)")
async def audit(
    admin: Annotated[User, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    action: Annotated[str | None, Query(max_length=64)] = None,
    only_failed: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """Paginated audit trail (``login_fail``, ``admin_block``, ``broadcast`` …)."""
    del admin
    conditions = []
    if action:
        conditions.append(AuditLog.action == action)
    if only_failed:
        conditions.append(AuditLog.success.is_(False))

    statement = select(AuditLog).order_by(desc(AuditLog.created_at), desc(AuditLog.id))
    count_statement = select(func.count()).select_from(AuditLog)
    for condition in conditions:
        statement = statement.where(condition)
        count_statement = count_statement.where(condition)
    statement = statement.limit(page_size).offset((page - 1) * page_size)

    rows = (await session.scalars(statement)).all()
    total = int(await session.scalar(count_statement) or 0)
    return {
        "items": [row.to_dict() for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, -(-total // page_size)),
    }


@router.post("/flags/{key}", response_model=MessageResponse, summary="⚙️ Toggle a live flag (admin)")
async def set_system_flag(
    key: str,
    admin: Annotated[User, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    value: Annotated[bool, Query()] = True,
) -> MessageResponse:
    """Flip ``auth_required`` / ``auth_allow_register`` without a restart.

    The value is written to ``system_flags`` (so it survives a redeploy) and
    applied to the in-process settings object immediately.
    """
    if key not in {FLAG_AUTH_REQUIRED, FLAG_ALLOW_REGISTER}:
        raise HTTPException(
            status_code=400,
            detail={"code": "unknown_flag", "message": f"Unsupported flag: {key}"},
        )

    await set_flag(session, key, value, updated_by=admin.telegram_id)

    if key == FLAG_AUTH_REQUIRED:
        settings.auth.required = value
        from bot.middlewares.auth_guard import set_auth_override

        set_auth_override(value)
    else:
        settings.auth.allow_registration = value

    await session.add(
        AuditLog.create(
            "admin_auth_toggle" if key == FLAG_AUTH_REQUIRED else "admin_register_toggle",
            actor_id=admin.telegram_id,
            detail=f"{key} -> {value}",
            extra={"key": key, "value": value},
            source="api",
        )
    )
    await session.commit()
    logger.info("Flag %s set to %s by %s", key, value, admin.telegram_id)
    return MessageResponse(ok=True, message=f"{key} = {value}")


@router.post("/broadcast", response_model=MessageResponse, summary="📢 Broadcast (admin)")
async def broadcast(
    admin: Annotated[User, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    text: Annotated[str, Query(min_length=1, max_length=4096)],
    dry_run: Annotated[bool, Query()] = False,
) -> MessageResponse:
    """Send a text message to every non-blocked user.

    Media broadcasts are far more convenient from ``/diyor`` inside Telegram
    (the bot copies the admin's message); this endpoint covers the text case and
    automated notifications. ``dry_run=true`` only counts the audience.
    """
    from aiogram import Bot
    from aiogram.exceptions import TelegramAPIError

    storage = StorageService(session, settings=settings)
    targets = await storage.broadcast_targets()
    if dry_run:
        return MessageResponse(ok=True, message=f"Audience: {len(targets)} users (dry run)")

    bot = Bot(token=settings.bot_token)
    sent = 0
    failed = 0
    try:
        for chat_id in targets:
            try:
                await bot.send_message(chat_id, text, parse_mode=settings.bot.parse_mode)
                sent += 1
            except TelegramAPIError as exc:
                failed += 1
                logger.info("Broadcast to %s failed: %s", chat_id, exc)
    finally:
        await bot.session.close()

    session.add(
        AuditLog.create(
            "broadcast",
            actor_id=admin.telegram_id,
            detail=f"api broadcast: {sent}/{len(targets)} delivered",
            extra={"sent": sent, "failed": failed, "total": len(targets)},
            source="api",
        )
    )
    await session.commit()
    return MessageResponse(
        ok=True, message=f"Delivered to {sent}/{len(targets)} users ({failed} failed)"
    )


@router.get("/counts", summary="Raw table counts (admin)")
async def counts(
    admin: Annotated[User, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, int]:
    """Quick row counts - handy when debugging a deployment."""
    del admin
    result: dict[str, int] = {}
    for label, model in (("users", User), ("files", File), ("folders", Folder), ("links", SharedLink), ("audit", AuditLog)):
        result[label] = int(await session.scalar(select(func.count()).select_from(model)) or 0)
    result["indexed_bytes"] = int(
        await session.scalar(select(func.coalesce(func.sum(File.file_size), 0))) or 0
    )
    return result


async def _storage_dep(session: Annotated[AsyncSession, Depends(get_db)]) -> StorageService:
    """Dependency wrapper (keeps the signature short in routes)."""
    return StorageService(session)
