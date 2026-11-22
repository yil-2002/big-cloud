"""``/auth`` — Mini App handshake, profile and preferences.

The Mini App sends ``WebApp.initData`` here once; in exchange it receives a JWT
that every other endpoint accepts as ``Authorization: Bearer <token>``.
"""

from __future__ import annotations

import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from api.dependencies import (
    get_cache_dep,
    get_current_user,
    get_optional_user,
    get_settings_dep,
    get_storage_service,
    throttle,
)
from api.schemas import (
    AuthResponse,
    LoginRequest,
    MessageResponse,
    RegisterRequest,
    SessionDeviceOut,
    SessionDevicesOut,
    StatsOut,
    TelegramAuthRequest,
    UserOut,
    UserSettingsPatch,
)
from api.security import (
    AuthError,
    InitDataError,
    active_session_id,
    active_session_key,
    create_session_token,
    decode_session_token,
    device_key,
    device_prefix,
    register_session,
    revocation_key,
    validate_init_data,
)
from api.serializers import serialize_stats, serialize_user
from config import Settings
from db.models import User
from services.auth import AuthOutcome, AuthService
from services.cache import Cache
from services.storage import StorageService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/telegram",
    response_model=AuthResponse,
    summary="Exchange Telegram initData for a session token",
    dependencies=[Depends(throttle("auth", limit=20, window=60))],
)
async def auth_telegram(
    payload: TelegramAuthRequest,
    request: Request,
    storage: Annotated[StorageService, Depends(get_storage_service)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    cache: Annotated[Cache, Depends(get_cache_dep)],
) -> AuthResponse:
    """Validate ``initData`` and issue a JWT.

    The user row is created on first contact, so opening the Mini App is enough
    to get a quota - the user does not have to press ``/start`` first.
    """
    client_ip = request.client.host if request.client else None
    try:
        parsed = validate_init_data(payload.init_data, settings=settings, client_ip=client_ip)
    except InitDataError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=exc.to_dict(),
            headers={"WWW-Authenticate": "TelegramInitData"},
        ) from exc

    tg_user = parsed.user
    if not payload.create_if_missing:
        existing = await storage.get_user(tg_user.id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "user_not_found", "message": "User is not registered"},
            )

    user, created = await storage.get_or_create_user(
        telegram_id=tg_user.id,
        username=tg_user.username,
        first_name=tg_user.first_name,
        last_name=tg_user.last_name,
        language_code=tg_user.language_code,
        is_premium=tg_user.is_premium,
    )
    if created:
        logger.info("Mini App registered user %s", tg_user.id)

    # Ko'p profilli sessiyalar: javob aktiv profil haqida; token esa Telegram
    # foydalanuvchisi nomidan (profil almashsa token yangilanmaydi — keyingi
    # so'rov avtomatik yangi profilga tushadi).
    from services.profiles import ProfileService

    user = await ProfileService(storage.session).resolve(user)

    # ``start_param`` is the deep-link payload (``?startapp=``) - the front-end
    # uses it to open a shared file straight away, so we echo it back.
    token = await _issue_session_token(
        request,
        user,
        cache=cache,
        settings=settings,
        start_param=parsed.start_param,
        subject_id=tg_user.id,
    )

    return AuthResponse(
        access_token=token,
        token=token,
        expires_in=settings.security.access_token_ttl,
        user=serialize_user(user, settings),
    )


@router.get("/me", response_model=UserOut, summary="Current profile and quota")
async def read_me(
    user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> UserOut:
    """Return the authenticated user with effective storage numbers."""
    return serialize_user(user, settings)


@router.patch("/me", response_model=UserOut, summary="Update preferences")
async def update_me(
    patch: UserSettingsPatch,
    user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[StorageService, Depends(get_storage_service)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> UserOut:
    """Merge a partial settings update (theme, sort order, toggles…)."""
    changes = patch.as_patch()
    if not changes:
        return serialize_user(user, settings)

    updated = await storage.update_settings(user.telegram_id, changes)
    return serialize_user(updated, settings)


@router.post("/logout", response_model=MessageResponse, summary="Invalidate this session")
async def logout(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    cache: Annotated[Cache, Depends(get_cache_dep)],
    storage: Annotated[StorageService, Depends(get_storage_service)],
) -> MessageResponse:
    """Close this session for real.

    Token'ning ``sid`` maydoni amal muddati tugagunicha **rad ro'yxatiga**
    yoziladi, shuning uchun nusxalangan token shu zahoti ishlamay qoladi.
    Ilgari bu endpoint amalda hech narsa qilmasdi: foydalanuvchi "chiqdi"
    deb o'ylardi, lekin token yana 24 soat ishlayverardi.
    """
    token = _bearer_token(request)
    if token:
        try:
            claims = decode_session_token(token, settings=settings)
        except AuthError:
            claims = {}
        session_id = str(claims.get("sid") or "")
        if session_id:
            lifetime = max(60, int(claims.get("exp") or 0) - int(time.time()))
            try:
                await cache.set(revocation_key(session_id), True, ttl=lifetime)
            except Exception:  # chiqish keshga bog'lanmasin
                logger.warning("Could not record session revocation", exc_info=True)

    # Profil sessiyasi ham yakunlanadi (botdagi /logout bilan bir xil): aks holda
    # Mini App qayta ochilganda foydalanuvchi o'sha profilga o'zi qaytib kirardi.
    if user.has_session_for(_caller_id(request, user)):
        service = AuthService(storage.session, storage=storage, settings=settings)
        await service.logout(user.telegram_id, source="api")

    return MessageResponse(ok=True, message="Session closed")


async def _issue_session_token(
    request: Request,
    user: User,
    *,
    cache: Cache,
    settings: Settings,
    start_param: str | None = None,
    subject_id: int | None = None,
) -> str:
    """Token yaratadi va uni akkauntning **yagona faol** sessiyasi qilib qo'yadi.

    Bitta akkaunt = bitta sessiya: eski sessiya bekor qilinadi, qurilma
    yozuvi yangilanadi. Foydalanuvchi qaysi qurilmada faol ekanini
    ``GET /auth/devices`` da ko'radi.
    """
    extra = {"start_param": start_param} if start_param else None
    # Token egasi — Telegram foydalanuvchisi (``subject_id``); Telegram'siz
    # mijozda profilning o'zi. Aktiv profil har so'rovda qayta aniqlanadi.
    subject = int(subject_id) if subject_id is not None else int(user.telegram_id)
    token = create_session_token(
        subject, username=user.username, settings=settings, extra_claims=extra
    )
    try:
        claims = decode_session_token(token, settings=settings)
        session_id = str(claims.get("sid") or "")
    except AuthError:  # pragma: no cover - yangi token har doim yaroqli
        session_id = ""
    if session_id:
        await register_session(
            cache,
            subject,
            session_id,
            ttl=settings.security.access_token_ttl,
            user_agent=request.headers.get("user-agent"),
            ip_address=request.client.host if request.client else None,
            username=user.username,
        )
    return token


def _caller_id(request: Request, user: User) -> int:
    """Sessiya egasi — chaqiruvchining Telegram ID si (aktiv profil ID emas)."""
    return int(getattr(request.state, "caller_telegram_id", None) or user.telegram_id)


def _bearer_token(request: Request) -> str:
    """``Authorization: Bearer <token>`` sarlavhasidan tokenni ajratish."""
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return value.strip()


# ---------------------------------------------------------------------------
# hugecloud account endpoints (login/password based)
# ---------------------------------------------------------------------------
@router.post(
    "/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a cloud account (login + password)",
    dependencies=[Depends(throttle("auth", limit=10, window=300))],
)
async def register_account(
    payload: RegisterRequest,
    request: Request,
    storage: Annotated[StorageService, Depends(get_storage_service)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    current: Annotated[User | None, Depends(get_optional_user)],
    cache: Annotated[Cache, Depends(get_cache_dep)],
) -> AuthResponse:
    """``/register`` ning HTTP ko'rinishi (Mini App hisob ekrani).

    Kim uchun hisob ochilayotgani ikki yo'l bilan aniqlanadi:

    1. ``X-Telegram-Init-Data`` sarlavhasi (Mini App har doim yuboradi);
    2. mavjud JWT (agar ilova avval ``/auth/telegram`` dan o'tgan bo'lsa).

    Ikkalasi ham bo'lmasa ``401`` qaytadi — anonim hisob yaratib bo'lmaydi.
    """
    telegram_id = await _resolve_telegram_id(request, current, settings)
    # Mini App'ni birinchi marta ochgan foydalanuvchining bazada qatori bo'lmasligi
    # mumkin (u botda /start bosmagan). Shuning uchun hisob ochishdan oldin
    # qatorni yaratib olamiz — aks holda 404 "not registered" qaytarilardi.
    if current is None:
        parsed_identity = _identity_from_init_data(request, settings)
        await storage.get_or_create_user(
            telegram_id,
            username=parsed_identity.user.username if parsed_identity else None,
            first_name=parsed_identity.user.first_name if parsed_identity else None,
            last_name=parsed_identity.user.last_name if parsed_identity else None,
            language_code=parsed_identity.user.language_code if parsed_identity else None,
            is_premium=bool(parsed_identity.user.is_premium) if parsed_identity else False,
        )
    service = AuthService(storage.session, storage=storage, settings=settings)
    result = await service.register(
        telegram_id,
        payload.login,
        payload.password,
        source="api",
        ip_address=request.client.host if request.client else None,
    )
    if not result.ok or result.user is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": result.outcome.value,
                "message": result.message or "Registration failed",
            },
        )

    token = await _issue_session_token(
        request, result.user, cache=cache, settings=settings, subject_id=telegram_id
    )
    return AuthResponse(
        access_token=token,
        token=token,
        expires_in=settings.security.access_token_ttl,
        user=serialize_user(result.user, settings),
    )


@router.post(
    "/login",
    response_model=AuthResponse,
    summary="Sign in with login + password",
    dependencies=[Depends(throttle("auth", limit=10, window=60))],
)
async def login_account(
    payload: LoginRequest,
    request: Request,
    storage: Annotated[StorageService, Depends(get_storage_service)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    cache: Annotated[Cache, Depends(get_cache_dep)],
) -> AuthResponse:
    """``/login`` ning HTTP ko'rinishi.

    Telegram ID aniqlansa, hisob shu akkauntga bog'lanadi (botdagi kabi).
    ID bo'lmasa (masalan, tashqi mijoz) faqat parol tekshiriladi va JWT beriladi.
    """
    telegram_id: int | None = None
    init_data = request.headers.get("X-Telegram-Init-Data")
    if init_data:
        try:
            parsed = validate_init_data(
                init_data,
                settings=settings,
                client_ip=request.client.host if request.client else None,
            )
            telegram_id = parsed.user.id
        except InitDataError:
            telegram_id = None

    service = AuthService(storage.session, storage=storage, settings=settings)
    result = await service.login(
        payload.login,
        payload.password,
        telegram_id=telegram_id,
        source="api",
        ip_address=request.client.host if request.client else None,
    )
    if not result.ok or result.user is None:
        status_code = {
            AuthOutcome.UNKNOWN_LOGIN: status.HTTP_404_NOT_FOUND,
            AuthOutcome.BLOCKED: status.HTTP_403_FORBIDDEN,
            AuthOutcome.LOCKED: status.HTTP_423_LOCKED,
        }.get(result.outcome, status.HTTP_401_UNAUTHORIZED)
        raise HTTPException(
            status_code=status_code,
            detail={
                "code": result.outcome.value,
                "message": result.message or "Authentication failed",
                "details": {
                    "attempts_left": result.attempts_left,
                    "lock_seconds": result.lock_seconds,
                },
            },
        )

    token = await _issue_session_token(
        request, result.user, cache=cache, settings=settings, subject_id=telegram_id
    )
    return AuthResponse(
        access_token=token,
        token=token,
        expires_in=settings.security.access_token_ttl,
        user=serialize_user(result.user, settings),
    )


@router.get(
    "/devices",
    response_model=SessionDevicesOut,
    summary="List active session (single-session policy)",
)
async def list_devices(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    cache: Annotated[Cache, Depends(get_cache_dep)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> SessionDevicesOut:
    """Joriy faol sessiya va qurilma ma'lumotini qaytaradi.

    Siyosat: bitta akkaunt = bitta sessiya, shuning uchun ro'yxatda odatda
    bitta yozuv bo'ladi. Eski qurilmalar yangi login paytida bekor qilinadi.
    """
    token = _bearer_token(request)
    current_sid = ""
    if token:
        try:
            current_sid = str(
                decode_session_token(token, settings=settings).get("sid") or ""
            )
        except AuthError:
            current_sid = ""
    if not current_sid:
        current_sid = str(await active_session_id(cache, _caller_id(request, user)) or "")

    devices: list[SessionDeviceOut] = []
    try:
        keys = await cache.backend.keys(f"{device_prefix(_caller_id(request, user))}*")
    except Exception:  # pragma: no cover - kesh yiqilsa bo'sh ro'yxat
        keys = []
    for key in sorted(keys):
        record = await cache.get(key)
        if not isinstance(record, dict):
            continue
        sid = str(record.get("sid") or key.rsplit(":", 1)[-1])
        devices.append(
            SessionDeviceOut(
                sid=sid,
                current=bool(current_sid) and sid == current_sid,
                user_agent=str(record.get("user_agent") or ""),
                ip=str(record.get("ip") or ""),
                username=str(record.get("username") or ""),
                created_at=int(record.get("created_at") or 0),
            )
        )
    return SessionDevicesOut(devices=devices, single_session=True)


@router.delete(
    "/devices/{session_id}",
    response_model=MessageResponse,
    summary="Revoke one session",
)
async def revoke_device(
    session_id: str,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    cache: Annotated[Cache, Depends(get_cache_dep)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> MessageResponse:
    """Berilgan sessiyani bekor qiladi (o'z joriy sessiyasini ham)."""
    record = await cache.get(device_key(_caller_id(request, user), session_id))
    if not isinstance(record, dict):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "session_not_found", "message": "Sessiya topilmadi"},
        )
    await cache.set(revocation_key(session_id), True, ttl=settings.security.access_token_ttl)
    await cache.delete(device_key(_caller_id(request, user), session_id))
    if await active_session_id(cache, _caller_id(request, user)) == session_id:
        await cache.delete(active_session_key(_caller_id(request, user)))
    return MessageResponse(ok=True, message="Sessiya yopildi")


@router.get("/me/stats", response_model=StatsOut, summary="My storage statistics")
async def my_stats(
    user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[StorageService, Depends(get_storage_service)],
) -> StatsOut:
    """``📊 Statistika`` ekrani uchun (fayllar, papkalar, kategoriyalar, kvota)."""
    return serialize_stats(await storage.stats(user.telegram_id))


def _identity_from_init_data(request: Request, settings: Settings):
    """``X-Telegram-Init-Data`` sarlavhasini tahlil qilish (xato bo'lsa ``None``)."""
    raw = request.headers.get("X-Telegram-Init-Data")
    if not raw:
        return None
    try:
        return validate_init_data(
            raw, settings=settings, client_ip=request.client.host if request.client else None
        )
    except InitDataError:
        return None


async def _resolve_telegram_id(
    request: Request, current: User | None, settings: Settings
) -> int:
    """Init-data / JWT dan Telegram ID ni aniqlash (ikkalasi yo'q bo'lsa 401)."""
    if current is not None:
        return current.telegram_id

    init_data = request.headers.get("X-Telegram-Init-Data")
    if init_data:
        try:
            parsed = validate_init_data(
                init_data,
                settings=settings,
                client_ip=request.client.host if request.client else None,
            )
            return parsed.user.id
        except InitDataError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=exc.to_dict()
            ) from exc

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "code": "identity_required",
            "message": "Send X-Telegram-Init-Data or an existing session token",
        },
    )
