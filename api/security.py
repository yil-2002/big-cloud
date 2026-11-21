"""Authentication & authorization primitives for the HTTP API.

Three responsibilities:

1. **Telegram Mini App ``initData`` validation** - the official algorithm
   (HMAC-SHA256 with ``secret_key = HMAC_SHA256("WebAppData", bot_token)``),
   including the ``auth_date`` freshness check and constant-time comparison.
2. **Session tokens** - compact HS256 JWTs minted after a successful
   ``initData`` validation (header ``Authorization: Bearer <token>``).
3. **Share pass codes** - re-exported salted-hash helpers so routes and the bot
   share one implementation.

The JWT is implemented on top of ``hmac``/``hashlib``/``base64`` from the standard
library: no third-party dependency, fully typed, and easy to audit.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl

from config import Settings, get_settings
from db.models.shared_link import (
    generate_pass_code,
    hash_pass_code,
    verify_pass_code,
)
from services.exceptions import AppError

logger = logging.getLogger(__name__)

__all__ = [
    "AuthError",
    "InitDataError",
    "TelegramInitData",
    "TelegramUserPayload",
    "create_session_token",
    "decode_session_token",
    "active_session_id",
    "active_session_key",
    "device_key",
    "device_prefix",
    "register_session",
    "revoke_user_sessions",
    "generate_pass_code",
    "hash_pass_code",
    "new_session_id",
    "revocation_key",
    "validate_init_data",
    "verify_pass_code",
]


class AuthError(AppError):
    """Any authentication/authorization failure (HTTP 401)."""

    status_code = 401
    code = "unauthorized"


class InitDataError(AuthError):
    """``initData`` is malformed, tampered with or too old."""

    code = "invalid_init_data"


@dataclass(frozen=True, slots=True)
class TelegramUserPayload:
    """The ``user`` object inside ``initData`` (subset we care about)."""

    id: int
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    language_code: str | None = None
    is_premium: bool = False
    allows_write_to_pm: bool = False
    photo_url: str | None = None


@dataclass(frozen=True, slots=True)
class TelegramInitData:
    """Validated Mini App payload."""

    user: TelegramUserPayload
    auth_date: int
    query_id: str | None = None
    chat_type: str | None = None
    chat_instance: str | None = None
    start_param: str | None = None
    can_send_after: int | None = None
    raw: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def telegram_id(self) -> int:
        """Shortcut used by routes and services."""
        return self.user.id

    @property
    def age_seconds(self) -> int:
        """How old the payload is (``now - auth_date``)."""
        return max(0, int(time.time()) - self.auth_date)


# ---------------------------------------------------------------------------
# initData validation
# ---------------------------------------------------------------------------
def _b64url_decode(value: str) -> bytes:
    """Decode base64url without padding (JWT style)."""
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _b64url_encode(raw: bytes) -> str:
    """Encode to base64url without padding."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _secret_key(bot_token: str) -> bytes:
    """``secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)``."""
    return hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()


def validate_init_data(
    init_data: str,
    *,
    bot_token: str | None = None,
    settings: Settings | None = None,
    max_age: int | None = None,
    client_ip: str | None = None,
) -> TelegramInitData:
    """Validate a Mini App ``initData`` string and return the parsed payload.

    Algorithm (as documented by Telegram):

    1. Parse ``initData`` as a query string.
    2. Extract ``hash``; build ``data_check_string`` from the remaining keys
       sorted alphabetically as ``key=value`` joined by ``\\n``.
    3. ``secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)``.
    4. Compare ``HMAC_SHA256(secret_key, data_check_string)`` with ``hash``
       using a constant-time comparison.
    5. Reject payloads whose ``auth_date`` is older than ``max_age``.

    Raises :class:`InitDataError` for every failure mode.
    """
    settings = settings or get_settings()
    token = bot_token or settings.bot_token
    limit = settings.security.init_data_max_age if max_age is None else max_age

    if not init_data or not init_data.strip():
        raise InitDataError("initData is empty")

    try:
        pairs = parse_qsl(init_data, strict_parsing=True, keep_blank_values=True)
    except ValueError as exc:
        raise InitDataError("initData is not a valid query string") from exc

    raw = dict(pairs)
    received_hash = raw.pop("hash", None)
    if not received_hash:
        raise InitDataError("initData has no hash")
    if "auth_date" not in raw:
        raise InitDataError("initData has no auth_date")

    data_check_string = "\n".join(f"{key}={raw[key]}" for key in sorted(raw))
    expected = hmac.new(
        _secret_key(token), data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, received_hash.strip()):
        logger.warning("initData signature mismatch (possible tampering)")
        raise InitDataError("initData signature is invalid")

    try:
        auth_date = int(raw["auth_date"])
    except (TypeError, ValueError) as exc:
        raise InitDataError("initData auth_date is not an integer") from exc

    if limit and (time.time() - auth_date) > limit:
        raise InitDataError("initData is too old", age=int(time.time() - auth_date))

    user_raw = raw.get("user")
    if not user_raw:
        raise InitDataError("initData has no user object")
    try:
        user_data: dict[str, Any] = json.loads(user_raw)
    except json.JSONDecodeError as exc:
        raise InitDataError("initData user is not valid JSON") from exc

    if "id" not in user_data:
        raise InitDataError("initData user has no id")

    if settings.security.bind_ip and client_ip:
        # Optional hardening: bind the payload to the connecting IP. Only enable
        # when the proxy chain reliably sets X-Forwarded-For.
        bound = raw.get("ip") or ""
        if bound and bound != client_ip:
            raise InitDataError("initData IP binding mismatch")

    user = TelegramUserPayload(
        id=int(user_data["id"]),
        first_name=user_data.get("first_name"),
        last_name=user_data.get("last_name"),
        username=user_data.get("username"),
        language_code=user_data.get("language_code"),
        is_premium=bool(user_data.get("is_premium", False)),
        allows_write_to_pm=bool(user_data.get("allows_write_to_pm", False)),
        photo_url=user_data.get("photo_url"),
    )

    return TelegramInitData(
        user=user,
        auth_date=auth_date,
        query_id=raw.get("query_id"),
        chat_type=raw.get("chat_type"),
        chat_instance=raw.get("chat_instance"),
        start_param=raw.get("start_param"),
        can_send_after=int(raw["can_send_after"]) if raw.get("can_send_after") else None,
        raw=raw,
    )


# ---------------------------------------------------------------------------
# Session tokens (compact HS256 JWT, stdlib only)
# ---------------------------------------------------------------------------
def new_session_id() -> str:
    """Opaque session id embedded in the token (allows future revocation)."""
    return secrets.token_urlsafe(12)


def revocation_key(session_id: str) -> str:
    """Bekor qilingan sessiya uchun kesh kaliti.

    ``/auth/logout`` shu kalitni yozadi, autentifikatsiya esa har so'rovda
    uni tekshiradi — shuning uchun token chiqishdan keyin darhol kuchini
    yo'qotadi.
    """
    return f"revoked_session:{session_id}"


# ---------------------------------------------------------------------------
# Bitta akkaunt = bitta sessiya (qurilmalar ro'yxati bilan)
# ---------------------------------------------------------------------------
def active_session_key(telegram_id: int) -> str:
    """Akkauntning **joriy** sessiya id'si saqlanadigan kesh kaliti."""
    return f"active_session:{int(telegram_id)}"


def device_key(telegram_id: int, session_id: str) -> str:
    """Bitta qurilma (sessiya) yozuvi uchun kesh kaliti."""
    return f"session_device:{int(telegram_id)}:{session_id}"


def device_prefix(telegram_id: int) -> str:
    """Akkauntning barcha qurilma yozuvlari prefiksi."""
    return f"session_device:{int(telegram_id)}:"


async def active_session_id(cache: Any, telegram_id: int) -> str | None:
    """Akkauntning joriy sessiya id'si (bo'lmasa ``None``).

    Kesh yiqilsa ``None`` qaytadi — autentifikatsiya keshga bog'lanib
    qolmasligi kerak (fail-open), xuddi revokatsiya tekshiruvi kabi.
    """
    try:
        value = await cache.get(active_session_key(telegram_id))
    except Exception:  # pragma: no cover - kesh xatosi so'rovni to'xtatmaydi
        return None
    return str(value) if value else None


async def register_session(
    cache: Any,
    telegram_id: int,
    session_id: str,
    *,
    ttl: int,
    user_agent: str | None = None,
    ip_address: str | None = None,
    username: str | None = None,
) -> str | None:
    """Yangi sessiyani **yagona faol** sessiya qiladi.

    Eski sessiya (agar mavjud bo'lsa) bekor qilinadi va qurilma yozuvlari
    yangilanadi. Qaytaradi: bekor qilingan eski sessiya id'si yoki ``None``.

    Siyosat: **bitta akkaunt — bitta sessiya**. Yangi login eski qurilmani
    chiqarib yuboradi; foydalanuvchi qaysi qurilmada faol ekanini
    ``GET /auth/devices`` orqali ko'radi.
    """
    import time as _time

    previous = await active_session_id(cache, telegram_id)
    try:
        if previous and previous != session_id:
            await cache.set(revocation_key(previous), True, ttl=max(60, int(ttl)))
        await cache.delete_prefix(device_prefix(telegram_id))
        await cache.set(
            device_key(telegram_id, session_id),
            {
                "sid": session_id,
                "user_agent": (user_agent or "")[:200],
                "ip": (ip_address or "")[:64],
                "username": username or "",
                "created_at": int(_time.time()),
            },
            ttl=max(60, int(ttl)),
        )
        await cache.set(active_session_key(telegram_id), session_id, ttl=max(60, int(ttl)))
    except Exception:  # pragma: no cover - kesh yiqilsa login baribir ishlaydi
        return previous
    return previous if previous != session_id else None


async def revoke_user_sessions(cache: Any, telegram_id: int) -> None:
    """Akkauntning barcha sessiyalarini bekor qiladi (masalan, hisob ko'chganda)."""
    current = await active_session_id(cache, telegram_id)
    try:
        if current:
            await cache.set(revocation_key(current), True, ttl=86400)
        await cache.delete(active_session_key(telegram_id))
        await cache.delete_prefix(device_prefix(telegram_id))
    except Exception:  # pragma: no cover - kesh xatosi oqimni to'xtatmaydi
        return None
    return None


def create_session_token(
    telegram_id: int,
    *,
    username: str | None = None,
    session_id: str | None = None,
    ttl: int | None = None,
    settings: Settings | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Mint an HS256 JWT for an authenticated Telegram user."""
    settings = settings or get_settings()
    lifetime = settings.security.access_token_ttl if ttl is None else ttl
    issued_at = int(time.time())

    header = {"alg": settings.security.jwt_algorithm, "typ": "JWT"}
    payload: dict[str, Any] = {
        "sub": str(telegram_id),
        "tg_id": telegram_id,
        "username": username,
        "sid": session_id or new_session_id(),
        "iat": issued_at,
        "exp": issued_at + max(60, lifetime),
        "iss": settings.app.name,
    }
    if extra_claims:
        payload.update(extra_claims)

    secret = settings.security.jwt_secret.get_secret_value().encode("utf-8")
    segments = [
        _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8")),
        _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
    ]
    signing_input = ".".join(segments).encode("ascii")
    signature = _b64url_encode(_hmac_for(settings.security.jwt_algorithm, secret, signing_input))
    return f"{segments[0]}.{segments[1]}.{signature}"


def decode_session_token(token: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """Verify a session token and return its claims.

    Raises :class:`AuthError` on a bad signature, unsupported algorithm or
    expiration. The ``alg`` header is validated against the configured value to
    prevent algorithm-confusion attacks (``none``/``HS512`` downgrades).
    """
    settings = settings or get_settings()
    if not token or token.count(".") != 2:
        raise AuthError("Malformed session token")

    header_b64, payload_b64, signature_b64 = token.split(".")
    secret = settings.security.jwt_secret.get_secret_value().encode("utf-8")

    try:
        header = json.loads(_b64url_decode(header_b64))
    except (ValueError, json.JSONDecodeError) as exc:
        raise AuthError("Malformed token header") from exc

    algorithm = header.get("alg")
    if algorithm != settings.security.jwt_algorithm:
        raise AuthError(f"Unsupported token algorithm: {algorithm!r}")

    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    expected = _b64url_encode(_hmac_for(str(algorithm), secret, signing_input))
    if not hmac.compare_digest(expected, signature_b64):
        raise AuthError("Invalid token signature")

    try:
        payload: dict[str, Any] = json.loads(_b64url_decode(payload_b64))
    except (ValueError, json.JSONDecodeError) as exc:
        raise AuthError("Malformed token payload") from exc

    expires_at = payload.get("exp")
    if isinstance(expires_at, int) and time.time() >= expires_at:
        raise AuthError("Session expired")

    if "tg_id" not in payload:
        raise AuthError("Token has no tg_id claim")
    return payload


def _hmac_for(algorithm: str, key: bytes, message: bytes) -> bytes:
    """HMAC digest for the supported ``HS*`` family."""
    mapping = {
        "HS256": hashlib.sha256,
        "HS384": hashlib.sha384,
        "HS512": hashlib.sha512,
    }
    digest = mapping.get(algorithm)
    if digest is None:  # pragma: no cover - guarded by decode_session_token
        raise AuthError(f"Unsupported HMAC algorithm: {algorithm!r}")
    return hmac.new(key, message, digest).digest()


# ---------------------------------------------------------------------------
# initData builders (tests, local development, documentation)
# ---------------------------------------------------------------------------
def build_init_data(
    *,
    telegram_id: int,
    username: str | None = "tester",
    first_name: str = "Test",
    bot_token: str,
    auth_date: int | None = None,
    start_param: str | None = None,
    is_premium: bool = False,
) -> str:
    """Create a **valid** ``initData`` string (used by tests and the dev harness).

    Mirrors what the Telegram WebApp SDK sends, so the same validator code path
    is exercised in tests as in production.
    """
    user = {
        "id": telegram_id,
        "first_name": first_name,
        "username": username,
        "language_code": "en",
        "is_premium": is_premium,
        "allows_write_to_pm": True,
    }
    fields: dict[str, str] = {
        "user": json.dumps(user, separators=(",", ":")),
        "auth_date": str(auth_date or int(time.time())),
        "chat_type": "private",
        "chat_instance": "1",
    }
    if start_param:
        fields["start_param"] = start_param

    data_check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    signature = hmac.new(
        _secret_key(bot_token), data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    query = "&".join(f"{key}={_quote(value)}" for key, value in fields.items())
    return f"{query}&hash={signature}"


def _quote(value: str) -> str:
    """Percent-encode a value the way ``parse_qsl`` expects to decode it."""
    from urllib.parse import quote

    return quote(value, safe="")


#: ``unquote`` is re-exported for callers that inspect raw initData manually.
__all__.append("unquote")
