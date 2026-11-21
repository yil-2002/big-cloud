"""Centralised, fully-typed application configuration.

Every tunable knob of the project lives here. Values are read from the process
environment and/or a ``.env`` file through :mod:`pydantic_settings`, validated on
startup and then exposed as a single immutable :class:`Settings` object.

Design notes
------------
* Nested ``BaseSettings`` models keep related options grouped (``settings.db.url``)
  while still supporting flat environment variables (``DATABASE_URL``) through the
  aliases defined below.
* ``get_settings()`` is cached with :func:`functools.lru_cache` so the whole app
  shares one instance, yet tests can call ``get_settings.cache_clear()``.
* No secret ever has a default value that would silently work in production:
  ``BOT_TOKEN`` is mandatory, everything else degrades gracefully for local dev.

Example
-------
>>> s = get_settings()
>>> s.db.url.startswith("postgresql+asyncpg://")
True
"""

from __future__ import annotations

import hashlib
import hmac
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import (
    AliasChoices,
    Field,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

__all__ = [
    "AppSettings",
    "BotSettings",
    "CacheSettings",
    "DatabaseSettings",
    "SecuritySettings",
    "Settings",
    "StorageSettings",
    "WebSettings",
    "get_settings",
]

#: Shared ``model_config``: read ``.env`` (UTF-8), ignore unknown keys, keep
#: nested models immutable after construction.
_BASE_CONFIG: SettingsConfigDict = {
    "env_file": ".env",
    "env_file_encoding": "utf-8",
    "case_sensitive": False,
    "extra": "ignore",
}


class AppSettings(BaseSettings):
    """Generic runtime flags shared by the bot and the HTTP API."""

    model_config = SettingsConfigDict(env_prefix="APP_", **_BASE_CONFIG)  # type: ignore[typeddict-item]

    name: str = Field(default="Telegram Cloud Drive", alias="APP_NAME")
    env: Literal["development", "staging", "production"] = Field(
        default="development", alias="APP_ENV"
    )
    debug: bool = Field(default=False, alias="APP_DEBUG")
    log_level: Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"] = Field(
        default="INFO", alias="LOG_LEVEL"
    )
    timezone: str = Field(default="UTC", alias="APP_TIMEZONE")
    #: UI language of bot messages: "uz" (legacy bot) or "en".
    language: Literal["uz", "en", "ru"] = Field(default="uz", alias="APP_LANGUAGE")
    #: Public base URL of the FastAPI service (used to build share/stream links).
    base_url: str = Field(default="http://localhost:8000", alias="BASE_URL")
    #: Semantic version, reported by ``GET /api/v1/system/version`` and ``/diyor``.
    version: str = Field(default="2.0.0", alias="APP_VERSION")

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        """Normalise ``https://host/`` -> ``https://host`` to keep joins clean."""
        return value.rstrip("/")

    @property
    def is_production(self) -> bool:
        """``True`` when running with ``APP_ENV=production``."""
        return self.env == "production"


class BotSettings(BaseSettings):
    """Telegram Bot API credentials and polling options."""

    model_config = SettingsConfigDict(env_prefix="BOT_", **_BASE_CONFIG)  # type: ignore[typeddict-item]

    #: Bot token from @BotFather. Mandatory - the app refuses to start without it.
    token: SecretStr = Field(alias=AliasChoices("BOT_TOKEN", "TELEGRAM_BOT_TOKEN"))
    #: Bot @username, required for inline mode hints and deep links.
    username: str = Field(default="your_cloud_bot", alias="BOT_USERNAME")
    #: Optional comma separated list of superuser Telegram IDs.
    #: ``NoDecode`` shart: aks holda pydantic-settings ``ADMIN_IDS=`` (bo'sh)
    #: qiymatini JSON deb o'qib, ``.env`` faylida xato bilan yiqiladi.
    admin_ids: Annotated[tuple[int, ...], NoDecode] = Field(default=(), alias="ADMIN_IDS")
    #: Primary superuser (``ADMIN_ID``). Merged into :attr:`admin_ids` so both the
    #: singular (legacy ``hugecloud``) and plural spellings work.
    admin_id: int | None = Field(default=None, alias="ADMIN_ID")
    #: Operator password for the ``/diyor`` admin panel. Empty = Telegram-ID check
    #: only (recommended for a single-owner deployment).
    admin_password: SecretStr = Field(default=SecretStr(""), alias="ADMIN_PASSWORD")
    parse_mode: Literal["HTML", "MarkdownV2"] | None = Field(
        default="HTML", alias="BOT_PARSE_MODE"
    )
    #: ``None`` lets aiogram drop updates received while the bot was offline.
    polling_timeout: PositiveInt = Field(default=30, alias="BOT_POLLING_TIMEOUT")
    polling_drop_pending: bool = Field(default=True, alias="BOT_DROP_PENDING_UPDATES")
    #: Maximum size (bytes) of a single message the bot may send/receive.
    max_message_size: NonNegativeInt = Field(
        default=50 * 1024 * 1024, alias="BOT_MAX_MESSAGE_SIZE"
    )

    # --- password gate (legacy behaviour of the original bot) -----------------
    #: Shared password every user must enter after ``/start``. Empty string
    #: disables the gate and makes the bot open to anyone who finds it.
    password: SecretStr = Field(
        default=SecretStr(""), alias=AliasChoices("BOT_PASSWORD", "BOT_ACCESS_PASSWORD")
    )
    #: When true, ``BOT_PASSWORD`` must be set or the app refuses to start
    #: (protects a public deployment from being accidentally left open).
    require_password: bool = Field(default=False, alias="BOT_REQUIRE_PASSWORD")
    #: How many wrong attempts are allowed before a short cooldown.
    password_max_attempts: PositiveInt = Field(default=5, alias="BOT_PASSWORD_ATTEMPTS")
    password_cooldown: PositiveInt = Field(default=60, alias="BOT_PASSWORD_COOLDOWN")

    @model_validator(mode="after")
    def _merge_admin_id(self) -> BotSettings:
        """``ADMIN_ID`` ni ``admin_ids`` ga qo'shish.

        Shunda butun kod bazasi (middleware, API, handlerlar) bitta
        ``settings.bot.admin_ids`` manbasidan foydalanadi va ``ADMIN_ID`` ham
        avtomatik hisobga olinadi.
        """
        if self.admin_id is not None and self.admin_id not in self.admin_ids:
            self.admin_ids = (*self.admin_ids, self.admin_id)
        return self

    def password_value(self) -> str:
        """Plain-text shared password (``""`` when the gate is disabled)."""
        return self.password.get_secret_value().strip()

    def admin_password_value(self) -> str:
        """Plain-text ``/diyor`` password (``""`` when ID-only auth is used)."""
        return self.admin_password.get_secret_value().strip()

    @property
    def all_admin_ids(self) -> tuple[int, ...]:
        """Every configured superuser id (``ADMIN_IDS`` + ``ADMIN_ID``)."""
        merged: list[int] = list(self.admin_ids)
        if self.admin_id is not None and self.admin_id not in merged:
            merged.append(self.admin_id)
        return tuple(merged)

    def is_admin(self, telegram_id: int | None) -> bool:
        """``True`` when the id belongs to a configured superuser."""
        return telegram_id is not None and telegram_id in self.all_admin_ids

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _split_admin_ids(cls, value: object) -> object:
        """Accept ``"1,2,3"`` / ``"[1,2,3]"`` / already parsed sequences."""
        if isinstance(value, str):
            cleaned = value.strip().lstrip("[").rstrip("]")
            if not cleaned:
                return ()
            return tuple(int(part) for part in cleaned.split(",") if part.strip())
        return value

    @field_validator("admin_id", mode="before")
    @classmethod
    def _clean_admin_id(cls, value: object) -> object:
        """Accept ``ADMIN_ID=""`` / ``"@"``-free strings without crashing."""
        if isinstance(value, str):
            cleaned = value.strip()
            return int(cleaned) if cleaned else None
        return value

    @field_validator("max_message_size", mode="before")
    @classmethod
    def _parse_size(cls, value: object) -> object:
        """Allow human friendly values such as ``50MB`` or ``2GB``."""
        if isinstance(value, str):
            return _human_size_to_bytes(value)
        return value

    @property
    def inline_mention(self) -> str:
        """``@username`` string used inside inline query placeholders."""
        return f"@{self.username.lstrip('@')}"

    @property
    def deep_link(self) -> str:
        """``https://t.me/<username>`` used to build ``?start=`` deep links."""
        return f"https://t.me/{self.username.lstrip('@')}"

    def start_link(self, payload: str) -> str:
        """Build a deep link: ``https://t.me/<bot>?start=<payload>``."""
        return f"{self.deep_link}?start={payload}"


class DatabaseSettings(BaseSettings):
    """SQLAlchemy async engine configuration."""

    model_config = SettingsConfigDict(env_prefix="DB_", **_BASE_CONFIG)  # type: ignore[typeddict-item]

    #: Full async DSN. PostgreSQL (asyncpg) in production, SQLite (aiosqlite)
    #: for zero-setup local development.
    url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/cloud_bot",
        alias=AliasChoices("DATABASE_URL", "DB_URL", "POSTGRES_DSN"),
    )
    #: Emit SQL statements to the log (never enable in production).
    echo: bool = Field(default=False, alias=AliasChoices("DB_ECHO", "SQLALCHEMY_ECHO"))
    #: Create missing tables on startup (handy for dev; use Alembic in prod).
    auto_create_tables: bool = Field(default=True, alias="DB_AUTO_CREATE")
    pool_size: PositiveInt = Field(default=10, alias="DB_POOL_SIZE")
    max_overflow: NonNegativeInt = Field(default=20, alias="DB_MAX_OVERFLOW")
    pool_timeout: PositiveInt = Field(default=30, alias="DB_POOL_TIMEOUT")
    pool_recycle: PositiveInt = Field(default=1800, alias="DB_POOL_RECYCLE")
    pool_pre_ping: bool = Field(default=True, alias="DB_POOL_PRE_PING")

    # --- SQLite pragmalari (faqat ``sqlite`` DSN'da qo'llanadi) --------------
    #: Journal rejimi. ``WAL`` bot va API bir vaqtda yozganda ``database is
    #: locked`` xatolarini keskin kamaytiradi va o'qishni yozishdan bloklamaydi.
    #: **Diqqat:** WAL tarmoq fayl tizimlarida (NFS/SMB) ishonchsiz — bunday
    #: holatda ``DELETE`` ga qaytaring.
    journal_mode: Literal["WAL", "DELETE", "TRUNCATE", "PERSIST", "MEMORY", "OFF"] = Field(
        default="WAL", alias="DB_JOURNAL_MODE"
    )
    #: Yozuv qulfini kutish muddati (ms). ``0`` bo'lsa ``SQLITE_BUSY`` darhol
    #: qaytadi; 5000 ms uzluksiz yozuvchi (bot + API + fon vazifasi) uchun yetarli.
    busy_timeout: NonNegativeInt = Field(default=5000, alias="DB_BUSY_TIMEOUT")
    #: ``synchronous``: WAL bilan ``NORMAL`` xavfsiz va sezilarli tezroq
    #: (``FULL`` har bir commit'da disk sinxronizatsiyasini talab qiladi).
    synchronous: Literal["OFF", "NORMAL", "FULL", "EXTRA"] = Field(
        default="NORMAL", alias="DB_SYNCHRONOUS"
    )

    @field_validator("url", mode="before")
    @classmethod
    def _normalise_driver(cls, value: object) -> object:
        """Upgrade sync driver names to their async equivalents."""
        if not isinstance(value, str):
            return value
        replacements = {
            "postgres://": "postgresql+asyncpg://",
            "postgresql://": "postgresql+asyncpg://",
            "sqlite://": "sqlite+aiosqlite://",
        }
        for sync, async_ in replacements.items():
            if value.startswith(sync) and not value.startswith(async_):
                value = async_ + value[len(sync):]
                break
        return _asyncpg_query_params(value)

    @property
    def is_sqlite(self) -> bool:
        """``True`` for SQLite backends (they need engine-specific options)."""
        return self.url.startswith("sqlite")

    #: Startup'da yetishmayotgan ustunlarni avtomatik qo'shish
    #: (``ALTER TABLE ADD COLUMN`` — ma'lumot o'chmaydi). Production'da
    #: ``false`` qo'yib, ``python scripts/migrate.py`` ni alohida bajaring.
    auto_migrate: bool = Field(default=True, alias="DB_AUTO_MIGRATE")


class CacheSettings(BaseSettings):
    """Redis connection + TTL policy. Redis is optional: when unreachable the
    app transparently falls back to an in-process cache and a memory rate limiter
    so local development never breaks."""

    model_config = SettingsConfigDict(env_prefix="REDIS_", **_BASE_CONFIG)  # type: ignore[typeddict-item]

    url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    #: Keep the app alive (degraded mode) when Redis is down instead of crashing.
    required: bool = Field(default=False, alias="REDIS_REQUIRED")
    max_connections: PositiveInt = Field(default=50, alias="REDIS_MAX_CONNECTIONS")
    socket_timeout: PositiveFloat = Field(default=3.0, alias="REDIS_SOCKET_TIMEOUT")
    socket_connect_timeout: PositiveFloat = Field(
        default=3.0, alias="REDIS_SOCKET_CONNECT_TIMEOUT"
    )
    #: Prefix for every key, so multiple deployments can share one Redis.
    key_prefix: str = Field(default="cloudbot", alias="REDIS_KEY_PREFIX")

    # --- TTL policy (seconds) -------------------------------------------------
    ttl_user: PositiveInt = Field(default=600, alias="CACHE_TTL_USER")
    ttl_file: PositiveInt = Field(default=300, alias="CACHE_TTL_FILE")
    ttl_search: PositiveInt = Field(default=60, alias="CACHE_TTL_SEARCH")
    #: Telegram file paths stay valid for ~1 hour; refresh a bit earlier.
    ttl_telegram_file_path: PositiveInt = Field(default=3000, alias="CACHE_TTL_TG_PATH")
    ttl_stream_token: PositiveInt = Field(default=300, alias="CACHE_TTL_STREAM_TOKEN")

    @field_validator("key_prefix")
    @classmethod
    def _sanitise_prefix(cls, value: str) -> str:
        """Strip separators that would corrupt key namespacing."""
        return value.strip().strip(":")


class ApiSettings(BaseSettings):
    """FastAPI / Uvicorn HTTP server options."""

    model_config = SettingsConfigDict(env_prefix="API_", **_BASE_CONFIG)  # type: ignore[typeddict-item]

    host: str = Field(default="0.0.0.0", alias="API_HOST")
    #: PaaS platforms (Render, Heroku, Fly) inject ``PORT``; it takes precedence
    #: over ``API_PORT`` so the same image runs anywhere without config edits.
    port: PositiveInt = Field(
        default=8000, alias=AliasChoices("PORT", "API_PORT")
    )
    workers: PositiveInt = Field(default=1, alias="API_WORKERS")
    api_prefix: str = Field(default="/api/v1", alias="API_PREFIX")
    #: ``NoDecode`` — ``CORS_ORIGINS=*`` JSON emas, vergul bilan ajratilgan ro'yxat.
    cors_origins: Annotated[tuple[str, ...], NoDecode] = Field(
        default=("*",), alias="CORS_ORIGINS"
    )
    docs_enabled: bool = Field(default=True, alias="API_DOCS_ENABLED")
    #: Serve the Telegram Mini App (``web/``) from the same process.
    serve_web: bool = Field(default=True, alias="SERVE_WEB")
    request_timeout: PositiveFloat = Field(default=30.0, alias="API_REQUEST_TIMEOUT")

    # --- webhook rejimi (PaaS'da polling o'rniga) ---------------------------
    #: ``true`` bo'lsa bot polling qilmaydi: Telegram yangilanishlarni shu
    #: ilovaga ``POST`` qiladi (bitta port, bitta jarayon).
    webhook_enabled: bool = Field(default=False, alias="WEBHOOK_ENABLED")
    #: Ommaviy URL, masalan ``https://hugecloud.onrender.com/api/webhook``.
    webhook_url: str = Field(default="", alias="WEBHOOK_URL")
    webhook_path: str = Field(default="/api/webhook", alias="WEBHOOK_PATH")
    #: Telegram ``X-Telegram-Bot-Api-Secret-Token`` sarlavhasi (soxta so'rovlarga qarshi).
    webhook_secret: str = Field(default="", alias="WEBHOOK_SECRET")

    @field_validator("webhook_path")
    @classmethod
    def _leading_slash(cls, value: str) -> str:
        """``api/webhook`` -> ``/api/webhook``."""
        return "/" + value.strip().strip("/")

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Accept a comma separated string for ``CORS_ORIGINS``."""
        if isinstance(value, str):
            return tuple(origin.strip() for origin in value.split(",") if origin.strip())
        return value

    @field_validator("api_prefix")
    @classmethod
    def _ensure_prefix_shape(cls, value: str) -> str:
        """Guarantee ``/api/v1`` style (leading slash, no trailing slash)."""
        value = "/" + value.strip().strip("/")
        return value if value != "/" else ""


class SecuritySettings(BaseSettings):
    """Auth, signing and throttling parameters."""

    model_config = SettingsConfigDict(env_prefix="SEC_", **_BASE_CONFIG)  # type: ignore[typeddict-item]

    #: Symmetric key used to sign JWT session tokens (min 32 chars).
    #:
    #: **Bo'sh qoldirilsa** :class:`Settings` uni ``BOT_TOKEN`` dan HMAC-SHA256
    #: orqali hosil qiladi — shunda hech qachon "hamma biladigan" sukut kalit
    #: ishlatilmaydi va sessiya tokenlarini soxtalashtirib bo'lmaydi.
    #: Bir necha servis bitta token bazasini bo'lishsa, aniq ``JWT_SECRET`` bering.
    jwt_secret: SecretStr = Field(default="", alias="JWT_SECRET")
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = Field(
        default="HS256", alias="JWT_ALGORITHM"
    )
    access_token_ttl: PositiveInt = Field(default=86400, alias="ACCESS_TOKEN_TTL")
    #: Reject Telegram ``initData`` older than this (seconds). ``0`` disables it.
    init_data_max_age: NonNegativeInt = Field(default=86400, alias="INIT_DATA_MAX_AGE")
    #: When set, ``initData.hash`` must also match ``sha256(auth_date + secret + ip)``
    #: style binding. Left ``None`` by default because clients sit behind proxies.
    bind_ip: bool = Field(default=False, alias="INIT_DATA_BIND_IP")

    # --- throttling -----------------------------------------------------------
    rate_limit_enabled: bool = Field(default=True, alias="RATE_LIMIT_ENABLED")
    #: API requests per user per window.
    api_rate_limit: PositiveInt = Field(default=60, alias="API_RATE_LIMIT")
    api_rate_window: PositiveInt = Field(default=60, alias="API_RATE_WINDOW")
    #: Telegram messages per user per window.
    bot_rate_limit: PositiveInt = Field(default=20, alias="BOT_RATE_LIMIT")
    bot_rate_window: PositiveInt = Field(default=60, alias="BOT_RATE_WINDOW")
    #: Button presses are cheaper but still limited to stop callback floods.
    callback_rate_limit: PositiveInt = Field(default=30, alias="CALLBACK_RATE_LIMIT")
    callback_rate_window: PositiveInt = Field(default=60, alias="CALLBACK_RATE_WINDOW")
    #: Inline queries hit the DB on every keystroke - keep this tight.
    inline_rate_limit: PositiveInt = Field(default=30, alias="INLINE_RATE_LIMIT")
    inline_rate_window: PositiveInt = Field(default=10, alias="INLINE_RATE_WINDOW")
    #: Downloads/stream requests are the most expensive operation.
    download_rate_limit: PositiveInt = Field(default=20, alias="DOWNLOAD_RATE_LIMIT")
    download_rate_window: PositiveInt = Field(default=60, alias="DOWNLOAD_RATE_WINDOW")
    #: ``/stream/{ticket}`` uchun alohida chegara (bandwidth-DoS himoyasi).
    #: Video pleer seek paytida ko'p Range so'rov yuboradi, shuning uchun
    #: chegara ataylab keng — lekin cheksiz emas: bitta manba daqiqasiga 120
    #: so'rovdan ortig'ini (skript/parallel yuklab olish belgisi) 429 oladi.
    stream_rate_limit: PositiveInt = Field(default=120, alias="STREAM_RATE_LIMIT")
    stream_rate_window: PositiveInt = Field(default=60, alias="STREAM_RATE_WINDOW")

    #: Minimum entropy (bytes) for generated share tokens / pass codes.
    token_entropy: PositiveInt = Field(default=24, alias="SHARE_TOKEN_ENTROPY")

    #: Legacy sukut qiymat — endi **ishlatilmaydi** (hammaga ma'lum konstanta).
    _legacy_default_secret: str = "change-me-in-production-please-32ch"

    @property
    def needs_derived_secret(self) -> bool:
        """``True`` bo'lsa kalit ``BOT_TOKEN`` dan hosil qilinishi kerak."""
        value = self.jwt_secret.get_secret_value()
        return not value or value == self._legacy_default_secret

    @model_validator(mode="after")
    def _validate_secret_strength(self) -> SecuritySettings:
        """Aniq berilgan kalit yetarlicha kuchli bo'lishi shart."""
        if not self.needs_derived_secret and len(self.jwt_secret.get_secret_value()) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters long.")
        return self


class AuthSettings(BaseSettings):
    """Per-user account authentication (``/register`` + ``/login``).

    This is the ``hugecloud`` access model: every account has its own login and
    password, and the :class:`~bot.middlewares.auth_guard.AuthGuardMiddleware`
    refuses cloud features until ``users.is_authenticated`` is true. It is
    independent from the *global* :class:`BotSettings` password gate, which can
    stay disabled (``BOT_PASSWORD=""``).
    """

    model_config = SettingsConfigDict(env_prefix="AUTH_", **_BASE_CONFIG)  # type: ignore[typeddict-item]

    #: Master switch. ``False`` = open bot (useful while testing).
    required: bool = Field(default=True, alias="AUTH_REQUIRED")
    #: Allow self-service ``/register``. Turn off for an invite-only deployment.
    allow_registration: bool = Field(default=True, alias="AUTH_ALLOW_REGISTER")
    #: Login names are matched case-insensitively when true.
    case_sensitive_login: bool = Field(default=False, alias="AUTH_CASE_SENSITIVE")
    #: Minimum password length enforced on ``/register``.
    min_password_length: PositiveInt = Field(default=6, alias="AUTH_MIN_PASSWORD")
    #: Maximum length accepted (bcrypt-style hashes truncate at 72 bytes).
    max_password_length: PositiveInt = Field(default=128, alias="AUTH_MAX_PASSWORD")
    #: Wrong attempts before a temporary lock (per user, in FSM data).
    max_attempts: PositiveInt = Field(default=5, alias="AUTH_MAX_ATTEMPTS")
    #: Lock duration in seconds.
    lockout_seconds: PositiveInt = Field(default=300, alias="AUTH_LOCKOUT")
    #: Keep a user signed in across restarts (persisted in ``users``).
    remember_sessions: bool = Field(default=True, alias="AUTH_REMEMBER")


class ArchiveSettings(BaseSettings):
    """Private archive channel - the persistence vault behind the cloud.

    Every upload is copied into ``ARCHIVE_CHANNEL_ID`` (the bot must be an admin
    there). The copy's ``message_id`` and channel ``file_id`` are stored on the
    file row, so the object survives even if the user deletes the original
    message or leaves the chat.
    """

    model_config = SettingsConfigDict(env_prefix="ARCHIVE_", **_BASE_CONFIG)  # type: ignore[typeddict-item]

    #: ``@privatechannel`` or a numeric ``-100xxxxxxxxxx`` id. Empty disables it.
    channel_id: str = Field(default="", alias=AliasChoices("ARCHIVE_CHANNEL_ID", "PRIVATE_ARCHIVE_CHANNEL"))
    #: Copy files into the vault. Disable to run without a channel.
    enabled: bool = Field(default=True, alias="ARCHIVE_ENABLED")
    #: Wait for the copy before confirming the upload (``False`` = fire-and-forget
    #: background task, the user sees the confirmation instantly).
    blocking: bool = Field(default=False, alias="ARCHIVE_BLOCKING")
    #: Retry a failed copy this many times before giving up.
    max_attempts: PositiveInt = Field(default=3, alias="ARCHIVE_MAX_ATTEMPTS")
    #: Delay between attempts (seconds, linear backoff).
    retry_delay: PositiveFloat = Field(default=2.0, alias="ARCHIVE_RETRY_DELAY")
    #: Prefix of the caption stored in the vault (audit trail).
    caption_prefix: str = Field(default="☁️ ARCHIVE", alias="ARCHIVE_CAPTION_PREFIX")

    @field_validator("channel_id", mode="before")
    @classmethod
    def _clean_channel(cls, value: object) -> object:
        """Normalise ``" -100123 "`` / ``""`` into a usable string."""
        if value is None:
            return ""
        return str(value).strip()

    @property
    def configured(self) -> bool:
        """``True`` when a channel is set and archiving is switched on."""
        return bool(self.enabled and self.channel_id)

    @property
    def numeric_id(self) -> int | None:
        """Numeric form of the channel id (``None`` for @usernames)."""
        try:
            return int(self.channel_id)
        except (TypeError, ValueError):
            return None


class StorageSettings(BaseSettings):
    """Cloud-storage product rules: quotas, upload limits, sharing defaults."""

    model_config = SettingsConfigDict(env_prefix="STORAGE_", **_BASE_CONFIG)  # type: ignore[typeddict-item]

    #: Default quota per user, in bytes. **``0`` means unlimited** - the
    #: ``hugecloud`` model stores everything on Telegram's servers, so there is no
    #: local disk cost and no artificial 5 GB ceiling. The UI renders ``0`` as
    #: "Cheksiz (Unlimited)".
    default_quota: NonNegativeInt = Field(
        default=0, alias=AliasChoices("STORAGE_QUOTA", "DEFAULT_QUOTA")
    )
    #: Bonus quota granted to admins (bytes). ``0`` = unlimited as well.
    admin_quota: NonNegativeInt = Field(default=0, alias="ADMIN_QUOTA")
    #: Label shown when the quota is unlimited.
    unlimited_label: str = Field(default="Cheksiz (Unlimited)", alias="QUOTA_UNLIMITED_LABEL")
    #: Hard cap for a single uploaded file (bytes). Telegram itself accepts up to
    #: 2 GB per document for bots, so the default matches the platform limit
    #: rather than the 20 MB *download* limit of the public Bot API.
    max_file_size: NonNegativeInt = Field(
        default=2 * 1024**3, alias="STORAGE_MAX_FILE_SIZE"
    )
    #: Telegram's own upload cap for bots (2 GB per document).
    telegram_upload_limit: NonNegativeInt = Field(
        default=2 * 1024**3, alias="TELEGRAM_UPLOAD_LIMIT"
    )
    #: Allow de-duplication of identical ``file_unique_id`` uploads.
    dedupe_enabled: bool = Field(default=True, alias="STORAGE_DEDUPE")
    #: New files are private unless the user flips the global switch.
    default_public: bool = Field(default=False, alias="STORAGE_DEFAULT_PUBLIC")
    #: Default lifetime of a generated share link (seconds). ``0`` = never expires.
    default_link_ttl: NonNegativeInt = Field(default=7 * 86400, alias="SHARE_LINK_TTL")
    #: How many items the inline query returns at once.
    inline_results_limit: PositiveInt = Field(default=20, alias="INLINE_RESULTS_LIMIT")
    #: Page size used by /files listing endpoints and inline keyboards.
    #: Default 5 matches the legacy bot's browsing experience.
    page_size: PositiveInt = Field(default=5, alias="PAGE_SIZE")
    #: Send the *actual media* when browsing (legacy behaviour) instead of a text
    #: list with buttons. Costs more messages but feels like the original bot.
    browse_send_media: bool = Field(default=True, alias="BROWSE_SEND_MEDIA")
    #: Proxy the Telegram file endpoint through our API (needed for Mini App
    #: playback). Disable to expose raw ``api.telegram.org`` URLs instead.
    stream_proxy_enabled: bool = Field(default=True, alias="STREAM_PROXY_ENABLED")
    #: **20 MB devori**: rasmiy Bot API ``getFile`` orqali botga faqat 20 MB
    #: gacha fayl yuklab beriladi (yuborish 2 GB bo'lsa ham!). Undan katta
    #: fayllar uchun ``/stream`` proksi baribir ishlamaydi — shuning uchun
    #: Telegram'ga murojaat qilmasdan oldin **413** qaytaramiz va Mini App
    #: foydalanuvchini "Telegram'da ochish" yo'liga yo'naltiradi (bot faylni
    #: ``file_id`` bo'yicha chatga cheklovsiz yubora oladi). ``0`` = devor
    #: o'chirilgan (masalan, lokal Bot API server ishlatilganda).
    stream_max_size: NonNegativeInt = Field(
        default=20 * 1024 * 1024, alias="STREAM_MAX_SIZE"
    )
    #: Local Bot API server base URL, e.g. ``http://localhost:8081``.
    local_bot_api_server: str | None = Field(default=None, alias="LOCAL_BOT_API")

    @field_validator("default_quota", "max_file_size", "telegram_upload_limit", mode="before")
    @classmethod
    def _human_sizes(cls, value: object) -> object:
        """Accept ``"5GB"``/``"20MB"`` strings as well as raw integers."""
        if isinstance(value, str):
            return _human_size_to_bytes(value)
        return value

    @field_validator("local_bot_api_server")
    @classmethod
    def _clean_local_api(cls, value: str | None) -> str | None:
        """Normalise the optional local Bot API base URL."""
        if not value:
            return None
        return value.strip().rstrip("/")


class WebSettings(BaseSettings):
    """Telegram Mini App (front-end) options."""

    model_config = SettingsConfigDict(env_prefix="WEB_", **_BASE_CONFIG)  # type: ignore[typeddict-item]

    #: Directory containing ``index.html`` (relative to the project root).
    static_dir: str = Field(default="web", alias="WEB_STATIC_DIR")
    mount_path: str = Field(default="/", alias="WEB_MOUNT_PATH")
    #: ``web_app_url`` reported to Telegram for the main menu button.
    web_app_url: str | None = Field(default=None, alias="WEB_APP_URL")
    #: Har startup'da ``setChatMenuButton`` chaqirilsinmi? Tunnel skriptlari
    #: ``.env`` ni yangilasa ham Telegram'ga yozmasdi — natijada Mini App har
    #: restartdan keyin o'lik manzilga ishora qilar edi. Shu sababli menyu
    #: tugmasi **har ishga tushishda** joriy ``WEB_APP_URL`` ga o'rnatiladi.
    set_menu_button: bool = Field(default=True, alias="WEB_SET_MENU_BUTTON")
    #: Menyu tugmasining ko'rinarli matni (Telegram cheklovi: 1-64 belgi).
    menu_button_text: str = Field(default="☁️ Bulut", alias="WEB_MENU_BUTTON_TEXT")

    @field_validator("mount_path")
    @classmethod
    def _ensure_leading_slash(cls, value: str) -> str:
        """Mount paths must start with ``/``."""
        return "/" + value.strip().strip("/")


class MtMediaSettings(BaseSettings):
    """MTProto configuration for large file streaming."""

    model_config = SettingsConfigDict(env_prefix="MT_", **_BASE_CONFIG)  # type: ignore[typeddict-item]

    enabled: bool = Field(default=False, alias="MT_MEDIA_ENABLED")
    api_id: int = Field(default=0, alias="TELEGRAM_API_ID")
    api_hash: str = Field(default="", alias="TELEGRAM_API_HASH")
    session_path: str = Field(default="/var/lib/bigcloud/mtproto-bot.session", alias="MT_SESSION_PATH")
    chunk_size: int = Field(default=1024 * 1024, alias="MT_CHUNK_BYTES")
    max_bytes: int = Field(default=2 * 1024**3, alias="MT_MAX_BYTES")

    @property
    def configured(self) -> bool:
        """True when MTProto is enabled and API credentials are provided."""
        return bool(self.enabled and self.api_id > 0 and self.api_hash)


class Settings(BaseSettings):
    """Root configuration object aggregating every section."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    app: AppSettings = Field(default_factory=AppSettings)
    bot: BotSettings = Field(default_factory=lambda: BotSettings())  # type: ignore[call-arg]
    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    archive: ArchiveSettings = Field(default_factory=ArchiveSettings)
    mt: MtMediaSettings = Field(default_factory=MtMediaSettings)

    @model_validator(mode="after")
    def _cross_section_defaults(self) -> Settings:
        """Derive convenience values that span several sections."""
        # The Mini App URL defaults to the API base URL when not set explicitly.
        if self.web.web_app_url is None:
            self.web.web_app_url = f"{self.app.base_url}{self.web.mount_path}".rstrip("/")
        # A production deployment must not be silently open to the whole world.
        if self.bot.require_password and not self.bot.password_value():
            raise ValueError(
                "BOT_REQUIRE_PASSWORD=true but BOT_PASSWORD is empty - "
                "set a password or disable the requirement."
            )
        # Auth enabled but registration closed and no accounts yet is a lockout.
        if self.auth.min_password_length > self.auth.max_password_length:
            raise ValueError("AUTH_MIN_PASSWORD cannot exceed AUTH_MAX_PASSWORD.")
        # 🔐 JWT kaliti: ``JWT_SECRET`` berilmagan bo'lsa, ``BOT_TOKEN`` dan
        # hosil qilinadi. Maqsad — deployment hech qachon "hammaga ma'lum"
        # sukut kalit bilan ishlamasligi (aks holda sessiya tokenlarini
        # soxtalashtirish mumkin edi).
        if self.security.needs_derived_secret:
            token = self.bot_token
            if not token:
                raise ValueError(
                    "JWT_SECRET yoki BOT_TOKEN dan biri shart: API sessiya "
                    "tokenlarini imzolash uchun kalit kerak."
                )
            derived = hmac.new(
                token.encode("utf-8"),
                b"hugecloud/jwt-secret/v1",
                hashlib.sha256,
            ).hexdigest()
            self.security.jwt_secret = SecretStr(derived)
        # Production'da kuchsiz kalit bilan ishga tushmaslik uchun ogohlantirish.
        elif self.app.env == "production" and len(self.security.jwt_secret.get_secret_value()) < 48:
            import warnings

            warnings.warn(
                "JWT_SECRET juda qisqa (production uchun kamida 48 belgi tavsiya etiladi).",
                stacklevel=2,
            )
        return self

    # --- shortcut proxies (flat env vars keep working) ------------------------
    @property
    def bot_token(self) -> str:
        """Plain-text bot token (use sparingly - prefer ``settings.bot.token``)."""
        return self.bot.token.get_secret_value()

    @property
    def database_url(self) -> str:
        """Alias for ``settings.db.url``."""
        return self.db.url

    @property
    def redis_url(self) -> str:
        """Alias for ``settings.cache.url``."""
        return self.cache.url

    @property
    def version(self) -> str:
        """Alias for ``settings.app.version``."""
        return self.app.version

    def describe(self) -> dict[str, object]:
        """Redacted snapshot, safe to log at startup."""
        return {
            "env": self.app.env,
            "debug": self.app.debug,
            "base_url": self.app.base_url,
            "bot_username": self.bot.username,
            "bot_token": "***" + self.bot_token[-4:] if self.bot_token else "<empty>",
            "database": _mask_dsn(self.db.url),
            "redis": _mask_dsn(self.cache.url),
            "quota_bytes": self.storage.default_quota,
            "quota_label": (
                self.storage.unlimited_label
                if self.storage.default_quota == 0
                else f"{self.storage.default_quota} bytes"
            ),
            "auth_required": self.auth.required,
            "archive_channel": self.archive.channel_id or "<disabled>",
            "admin_id": self.bot.admin_id,
            "max_file_size": self.storage.max_file_size,
            "rate_limit_enabled": self.security.rate_limit_enabled,
            "password_gate": bool(self.bot.password_value()),
            "language": self.app.language,
        }


_SIZE_UNITS: dict[str, int] = {
    "B": 1,
    "KB": 1024,
    "MB": 1024**2,
    "GB": 1024**3,
    "TB": 1024**4,
}


def _human_size_to_bytes(value: str) -> int:
    """Convert ``"20MB"``, ``"1.5 GB"``, ``"512"`` into an integer byte count."""
    text = value.strip().upper().replace(" ", "")
    for unit, multiplier in sorted(_SIZE_UNITS.items(), key=lambda kv: -len(kv[0])):
        if text.endswith(unit):
            number = text[: -len(unit)].rstrip("B") if unit != "B" else text[:-1]
            if not number:
                raise ValueError(f"Invalid size literal: {value!r}")
            return int(float(number) * multiplier)
    return int(float(text))


def _asyncpg_query_params(dsn: str) -> str:
    """Neon/Supabase DSN parametrlarini asyncpg tushunadigan ko'rinishga keltirish.

    Bulutli Postgres provayderlari ``?sslmode=require&channel_binding=require``
    beradi, lekin SQLAlchemy ularni ``asyncpg.connect()`` ga kwarg sifatida
    uzatadi va asyncpg ``sslmode`` ni tanimay yiqiladi. Shuning uchun:
    ``sslmode`` -> ``ssl``, ``channel_binding`` esa olib tashlanadi.
    """
    if not dsn.startswith("postgresql+asyncpg://") or "?" not in dsn:
        return dsn
    from urllib.parse import parse_qsl, urlencode

    base, query = dsn.split("?", 1)
    params: list[tuple[str, str]] = []
    for key, val in parse_qsl(query, keep_blank_values=True):
        if key == "channel_binding":
            continue
        if key == "sslmode":
            key = "ssl"
        params.append((key, val))
    return f"{base}?{urlencode(params)}" if params else base


def _mask_dsn(dsn: str) -> str:
    """Hide credentials inside a DSN before logging it."""
    if "://" not in dsn:
        return dsn
    scheme, remainder = dsn.split("://", 1)
    if "@" not in remainder:
        return dsn
    credentials, host = remainder.rsplit("@", 1)
    if ":" in credentials:
        user, _ = credentials.split(":", 1)
        credentials = f"{user}:***"
    else:
        credentials = "***"
    return f"{scheme}://{credentials}@{host}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` singleton (cached)."""
    return Settings()
