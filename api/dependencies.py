"""FastAPI dependencies: sessions, services, authentication, throttling.

Every route declares *what* it needs; this module decides *how* it is built.
That keeps routes short and makes the whole API testable by overriding a single
dependency (``app.dependency_overrides``).

Authentication accepts two equivalent credentials:

* ``Authorization: Bearer <JWT>`` — the normal path for the Mini App after
  ``POST /auth/telegram``;
* ``X-Telegram-Init-Data: <initData>`` — validated on every request, useful for
  the very first call and for clients that prefer not to store a token.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings, get_settings
from db.database import get_session_factory
from db.models import User
from services.cache import Cache, get_cache
from services.exceptions import AppError, NotOwnerError, UserNotFoundError
from services.rate_limiter import RateLimiter, get_rate_limiter
from services.share import ShareService
from services.storage import StorageService

logger = logging.getLogger(__name__)

__all__ = [
    "get_cache_dep",
    "get_current_user",
    "get_db",
    "get_limiter",
    "get_optional_user",
    "get_settings_dep",
    "get_share_service",
    "get_storage_service",
    "get_telegram_id",
    "get_telegram_id_optional",
    "require_admin",
    "throttle",
]

#: ``auto_error=False`` so we can fall back to the initData header ourselves.
_bearer_scheme = HTTPBearer(auto_error=False, description="JWT issued by POST /auth/telegram")


# ---------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield one ``AsyncSession`` per request and always close it."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        try:
            yield session
        except Exception:
            # Services own commits; make sure a failed request cannot leak an
            # open transaction back into the pool.
            if session.in_transaction():
                await session.rollback()
            raise


def get_settings_dep() -> Settings:
    """Application settings (cached singleton)."""
    return get_settings()


def get_cache_dep() -> Cache:
    """Cache facade (Redis or memory)."""
    return get_cache()


def get_limiter() -> RateLimiter:
    """Shared rate limiter."""
    return get_rate_limiter()


def get_storage_service(session: Annotated[AsyncSession, Depends(get_db)]) -> StorageService:
    """Storage business logic bound to the request session."""
    return StorageService(session, cache=get_cache(), settings=get_settings())


def get_share_service(session: Annotated[AsyncSession, Depends(get_db)]) -> ShareService:
    """Sharing business logic bound to the request session."""
    return ShareService(session, cache=get_cache(), settings=get_settings())


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
def _identity_from_credentials(
    credentials: HTTPAuthorizationCredentials | None,
    init_data: str | None,
    settings: Settings,
    client_ip: str | None,
) -> int:
    """Resolve the ``telegram_id`` from a JWT or a raw ``initData`` string."""
    from api.security import AuthError, decode_session_token, validate_init_data

    if credentials is not None and credentials.credentials:
        try:
            claims = decode_session_token(credentials.credentials, settings=settings)
        except AuthError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=exc.to_dict(),
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        return int(claims["tg_id"])

    if init_data:
        from api.security import InitDataError

        try:
            parsed = validate_init_data(init_data, settings=settings, client_ip=client_ip)
        except InitDataError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=exc.to_dict(),
                headers={"WWW-Authenticate": "TelegramInitData"},
            ) from exc
        return parsed.telegram_id

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "unauthorized", "message": "Missing credentials"},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_telegram_id(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)] = None,
    x_telegram_init_data: Annotated[str | None, Header(alias="X-Telegram-Init-Data")] = None,
    settings: Annotated[Settings, Depends(get_settings_dep)] = None,  # type: ignore[assignment]
    cache: Annotated[Cache, Depends(get_cache_dep)] = None,  # type: ignore[assignment]
) -> int:
    """Return the authenticated ``telegram_id`` (JWT preferred, initData fallback).

    JWT ishlatilganda sessiya **bekor qilingan** emasligi ham tekshiriladi:
    ``/auth/logout`` tokenni rad ro'yxatiga qo'shadi va shu yerdan o'tgan
    har bir so'rov uni ko'radi. Ilgari chiqish amalda hech narsa
    qilmasdi va token amal muddati tugagunicha ishlayverardi.
    """
    settings = settings or get_settings()
    client_ip = request.client.host if request.client else None
    telegram_id = _identity_from_credentials(
        credentials, x_telegram_init_data, settings, client_ip
    )
    if credentials is not None:
        await _reject_if_revoked(credentials.credentials, settings, cache)
    return telegram_id


async def _reject_if_revoked(token: str, settings: Settings, cache: Cache | None) -> None:
    """Bekor qilingan sessiya tokenini ``401`` bilan rad etish.

    Ikki qoida tekshiriladi:

    1. ``sid`` rad ro'yxatida bo'lsa (``/auth/logout`` yoki boshqa qurilma
       kirganda yoziladi) — token ishlamaydi;
    2. **bitta akkaunt = bitta sessiya**: akkauntning joriy sessiyasi boshqa
       bo'lsa, bu token eski qurilmadan — rad etiladi.

    Ikkalasi ham fail-open: kesh yiqilsa so'rov to'xtamaydi.
    """
    if cache is None:
        return
    from api.security import (
        AuthError,
        active_session_id,
        decode_session_token,
        revocation_key,
    )

    try:
        claims = decode_session_token(token, settings=settings)
    except AuthError:
        # Imzo va amal muddati allaqachon tekshirilgan.
        return
    session_id = str(claims.get("sid") or "")
    if not session_id:
        return
    try:
        revoked = await cache.get(revocation_key(session_id))
        if revoked:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "session_revoked", "message": "Session was closed"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        telegram_id = int(claims.get("tg_id") or 0)
        if telegram_id:
            current = await active_session_id(cache, telegram_id)
            if current and current != session_id:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={
                        "code": "session_replaced",
                        "message": "Sessiya boshqa qurilmada ochilgan.",
                    },
                    headers={"WWW-Authenticate": "Bearer"},
                )
    except HTTPException:
        raise
    except Exception:  # kesh yiqilsa so'rov to'xtamasin
        logger.warning("Session revocation check failed", exc_info=True)
        return


async def get_telegram_id_optional(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)] = None,
    x_telegram_init_data: Annotated[str | None, Header(alias="X-Telegram-Init-Data")] = None,
    settings: Annotated[Settings, Depends(get_settings_dep)] = None,  # type: ignore[assignment]
) -> int | None:
    """Anonim so'rovda ``None`` qaytaradigan :func:`get_telegram_id`.

    ``POST /auth/login`` va ``/auth/register`` kabi **ommaviy** endpointlarda
    identifikator hali yo'q; shuning uchun limitlash IP bo'yicha davom etadi
    (aks holda butun auth router'i 401 bilan yopilib qoladi).
    """
    settings = settings or get_settings()
    client_ip = request.client.host if request.client else None
    try:
        return _identity_from_credentials(
            credentials, x_telegram_init_data, settings, client_ip
        )
    except HTTPException:
        return None


async def get_current_user(
    request: Request,
    telegram_id: Annotated[int, Depends(get_telegram_id)],
    storage: Annotated[StorageService, Depends(get_storage_service)],
) -> User:
    """Load (or register) the ``User`` row of the caller.

    Registering on first API contact mirrors the bot's behaviour, so a user who
    opens the Mini App without ever pressing ``/start`` still gets a row and a
    quota.
    """
    try:
        user = await storage.get_user(telegram_id)
    except Exception as exc:
        logger.exception("Could not load user %s", telegram_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "storage_unavailable", "message": str(exc)},
        ) from exc

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=UserNotFoundError(f"User {telegram_id} is not registered").to_dict(),
        )

    # --- API darvozalari (bot AuthGuardMiddleware bilan bir xil qoidalar) ----
    # Bloklangan hisob hech qachon o'tmaydi; hisob tasdiqlanmagan (login/parol
    # qilinmagan) bo'lsa esa faqat AUTH_REQUIRED yoqilganda rad etiladi —
    # adminlar bundan mustasno (botdagi xatti-harakat bilan parita).
    #
    # ``/auth/*`` yo'llari istisno: Mini App login oqimi ``GET /auth/me`` ni
    # hisob tasdiqlanmasidan OLDIN chaqiradi (login/register ekranini shu javob
    # boshqaradi) — darvoza u yerda turib qolsa ro'yxatdan o'tishning o'zi
    # imkonsiz bo'lib qoladi. Bloklash esa hamma joyda amal qiladi.
    settings = get_settings()
    if user.is_blocked:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "blocked", "message": "Hisobingiz bloklangan."},
        )
    is_auth_route = "/auth/" in request.url.path
    is_admin = settings.bot.is_admin(user.telegram_id)
    if (
        settings.auth.required
        and not user.is_authenticated
        and not is_admin
        and not is_auth_route
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "not_authenticated",
                "message": "Avval hisobni tasdiqlang: /login yoki /register.",
            },
        )
    return user


async def get_optional_user(
    request: Request,
    storage: Annotated[StorageService, Depends(get_storage_service)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)] = None,
    x_telegram_init_data: Annotated[str | None, Header(alias="X-Telegram-Init-Data")] = None,
    settings: Annotated[Settings, Depends(get_settings_dep)] = None,  # type: ignore[assignment]
) -> User | None:
    """Like :func:`get_current_user` but returns ``None`` instead of raising.

    Used by ``POST /auth/register`` where the caller may legitimately have no
    session yet - the route falls back to ``X-Telegram-Init-Data`` and answers
    ``401`` itself with a precise message.
    """
    settings = settings or get_settings()
    client_ip = request.client.host if request.client else None
    try:
        telegram_id = _identity_from_credentials(
            credentials, x_telegram_init_data, settings, client_ip
        )
    except HTTPException:
        return None
    try:
        return await storage.get_user(telegram_id)
    except Exception:
        logger.debug("get_optional_user: no row for %s", telegram_id)
        return None


async def require_admin(
    user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> User:
    """Restrict a route to configured superusers (``ADMIN_IDS``)."""
    if user.telegram_id not in settings.bot.admin_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=NotOwnerError("Administrator access required").to_dict(),
        )
    return user


# ---------------------------------------------------------------------------
# Throttling
# ---------------------------------------------------------------------------
def throttle(
    scope: str, *, limit: int | None = None, window: int | None = None
) -> Callable[..., Awaitable[None]]:
    """Dependency factory enforcing a per-user sliding window.

    ``limit``/``window`` default to the configured values for ``scope``
    (``api``, ``download``, ``callback`` …). On denial the caller receives a
    standard ``429`` with ``Retry-After`` and ``RateLimit-*`` headers.
    """

    async def _dependency(
        request: Request,
        response: Response,
        telegram_id: Annotated[int | None, Depends(get_telegram_id_optional)],
        limiter: Annotated[RateLimiter, Depends(get_limiter)],
        settings: Annotated[Settings, Depends(get_settings_dep)],
    ) -> None:
        if not settings.security.rate_limit_enabled:
            return

        configured_limit, configured_window = _limits_for(scope, settings)
        # Foydalanuvchi aniqlanmagan bo'lsa (login/register) — IP bo'yicha
        # limitlaymiz: anonim traffic ham nazoratsiz qolmasligi kerak.
        client_ip = request.client.host if request.client else "unknown"
        subject = f"u{telegram_id}" if telegram_id is not None else f"ip{client_ip}"
        result = await limiter.check(
            scope,
            subject,
            limit=limit or configured_limit,
            window=window or configured_window,
        )
        # FastAPI merges headers set on an injected Response into the final one,
        # so every throttled endpoint advertises its remaining budget.
        for key, value in result.as_headers().items():
            response.headers[key] = value

        if not result.allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "rate_limited",
                    "message": "Too many requests",
                    "details": {"retry_after": result.retry_after, "scope": scope},
                },
                headers=result.as_headers(),
            )

    return _dependency


def _limits_for(scope: str, settings: Settings) -> tuple[int, int]:
    """Configured ``(limit, window)`` for a throttling scope."""
    security = settings.security
    if scope == "download":
        return security.download_rate_limit, security.download_rate_window
    if scope == "inline":
        return security.inline_rate_limit, security.inline_rate_window
    if scope == "stream":
        return security.stream_rate_limit, security.stream_rate_window
    return security.api_rate_limit, security.api_rate_window


def error_detail(exc: AppError) -> dict[str, Any]:
    """Uniform error body used by every ``HTTPException`` in this module."""
    return exc.to_dict()


def pagination_params(
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> tuple[int, int]:
    """Shared pagination query parameters."""
    return page, page_size
