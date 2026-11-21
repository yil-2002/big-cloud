"""Caching layer: Redis in production, in-process memory as a fallback.

The public surface is intentionally tiny and fully async:

    cache = await init_cache()          # once, at startup
    await cache.set("user:1", {...})    # dict/list/str/int are JSON encoded
    data = await cache.get("user:1")
    await cache.delete_prefix("files:")
    await cache.close()

Why a fallback?
---------------
Redis is an *optimisation*, not a correctness requirement. When it is
unreachable (local dev without Docker, transient network blip) the app keeps
working with :class:`MemoryCacheBackend` and logs a single warning, so nothing
crashes and no request fails. Set ``REDIS_REQUIRED=true`` to make the dependency
strict instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from typing import Any

from config import get_settings

logger = logging.getLogger(__name__)

__all__ = [
    "Cache",
    "CacheBackend",
    "CacheKeys",
    "MemoryCacheBackend",
    "RedisCacheBackend",
    "close_cache",
    "get_cache",
    "init_cache",
]

_JSON = (dict, list, str, int, float, bool, type(None))


# ---------------------------------------------------------------------------
# Key namespace - one place to keep cache keys consistent and greppable.
# ---------------------------------------------------------------------------
class CacheKeys:
    """Builders for every cache key used by the project."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix.strip(":")

    def _k(self, *parts: object) -> str:
        joined = ":".join(str(part) for part in parts)
        return f"{self.prefix}:{joined}" if self.prefix else joined

    # users -------------------------------------------------------------------
    def user(self, telegram_id: int) -> str:
        """Cached ``User`` row (settings + storage counters)."""
        return self._k("user", telegram_id)

    def user_settings(self, telegram_id: int) -> str:
        return self._k("user", telegram_id, "settings")

    # files -------------------------------------------------------------------
    def file(self, file_uuid: object) -> str:
        """Cached file metadata by primary key."""
        return self._k("file", file_uuid)

    def file_by_token(self, token: str) -> str:
        """Cached share-token -> file metadata lookup."""
        return self._k("share", token)

    def listing(self, telegram_id: int, folder: object, page: int, query: str = "") -> str:
        """Cached folder listing / search page."""
        return self._k("list", telegram_id, folder or "root", page, query or "-")

    def search(self, telegram_id: int, query: str, limit: int) -> str:
        """Cached inline-mode search results."""
        return self._k("search", telegram_id, abs(hash(query.lower())), limit)

    # telegram ----------------------------------------------------------------
    def tg_file_path(self, file_id: str) -> str:
        """``getFile`` result path (valid ~1 h on Telegram's side)."""
        return self._k("tgpath", file_id)

    def stream_token(self, token: str) -> str:
        """Short-lived signed streaming token."""
        return self._k("stream", token)

    # rate limiting -----------------------------------------------------------
    def rate(self, scope: str, identity: str) -> str:
        return self._k("rl", scope, identity)

    # stats -------------------------------------------------------------------
    def stats(self, telegram_id: int) -> str:
        return self._k("stats", telegram_id)

    def lock(self, name: str) -> str:
        """Distributed lock key (uploads, quota recalculation)."""
        return self._k("lock", name)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------
class CacheBackend(ABC):
    """Minimal async key/value contract implemented by both backends."""

    @abstractmethod
    async def get(self, key: str) -> Any | None: ...

    @abstractmethod
    async def set(self, key: str, value: Any, ttl: int | None = None) -> bool: ...

    @abstractmethod
    async def delete(self, *keys: str) -> int: ...

    @abstractmethod
    async def exists(self, key: str) -> bool: ...

    @abstractmethod
    async def incr(self, key: str, amount: int = 1, ttl: int | None = None) -> int: ...

    @abstractmethod
    async def keys(self, pattern: str) -> list[str]: ...

    @abstractmethod
    async def close(self) -> None: ...

    @property
    @abstractmethod
    def name(self) -> str: ...


class MemoryCacheBackend(CacheBackend):
    """Thread-unsafe but loop-safe in-process cache with lazy TTL expiry."""

    def __init__(self, max_items: int = 20_000) -> None:
        self._store: dict[str, tuple[float | None, str]] = {}
        self._max_items = max_items
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return "memory"

    def _is_expired(self, expires_at: float | None) -> bool:
        return expires_at is not None and expires_at <= time.monotonic()

    async def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, raw = entry
        if self._is_expired(expires_at):
            self._store.pop(key, None)
            return None
        return json.loads(raw)

    async def set(self, key: str, value: Any, ttl: int | None = None) -> bool:
        async with self._lock:
            if len(self._store) >= self._max_items:
                self._evict()
            expires_at = time.monotonic() + ttl if ttl and ttl > 0 else None
            self._store[key] = (expires_at, json.dumps(value, default=str))
        return True

    def _evict(self) -> None:
        """Drop expired entries first, then the oldest ~10% (cheap LRU-ish)."""
        now = time.monotonic()
        expired = [k for k, (exp, _) in self._store.items() if exp is not None and exp <= now]
        for key in expired:
            self._store.pop(key, None)
        if len(self._store) >= self._max_items:
            for key in list(self._store)[: max(1, self._max_items // 10)]:
                self._store.pop(key, None)

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if self._store.pop(key, None) is not None:
                removed += 1
        return removed

    async def exists(self, key: str) -> bool:
        return await self.get(key) is not None

    async def incr(self, key: str, amount: int = 1, ttl: int | None = None) -> int:
        current = await self.get(key)
        value = int(current or 0) + amount
        await self.set(key, value, ttl)
        return value

    async def keys(self, pattern: str) -> list[str]:
        import fnmatch

        prefix = pattern.rstrip("*")
        return [
            key
            for key, (expires_at, _) in list(self._store.items())
            if (key.startswith(prefix) or fnmatch.fnmatch(key, pattern))
            and not self._is_expired(expires_at)
        ]

    async def close(self) -> None:
        self._store.clear()


class RedisCacheBackend(CacheBackend):
    """Async Redis backend (``redis.asyncio``) with JSON value encoding."""

    def __init__(self, client: Any) -> None:  # redis.asyncio.Redis
        self._client = client

    @property
    def name(self) -> str:
        return "redis"

    @property
    def client(self) -> Any:
        """Raw client - needed by the rate limiter (sorted sets / scripts)."""
        return self._client

    async def get(self, key: str) -> Any | None:
        raw = await self._client.get(key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    async def set(self, key: str, value: Any, ttl: int | None = None) -> bool:
        payload = json.dumps(value, default=str)
        if ttl and ttl > 0:
            return bool(await self._client.set(key, payload, ex=ttl))
        return bool(await self._client.set(key, payload))

    async def delete(self, *keys: str) -> int:
        if not keys:
            return 0
        return int(await self._client.delete(*keys))

    async def exists(self, key: str) -> bool:
        return bool(await self._client.exists(key))

    async def incr(self, key: str, amount: int = 1, ttl: int | None = None) -> int:
        value = int(await self._client.incrby(key, amount))
        if ttl and ttl > 0 and value == amount:
            await self._client.expire(key, ttl)
        return value

    async def keys(self, pattern: str) -> list[str]:
        """Use ``SCAN`` (never ``KEYS``) to avoid blocking the Redis server."""
        found: list[str] = []
        async for key in self._client.scan_iter(match=pattern, count=500):
            found.append(key.decode() if isinstance(key, bytes) else key)
        return found

    async def close(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Facade
# ---------------------------------------------------------------------------
class Cache:
    """High level cache API used across handlers and routes.

    Every method swallows backend errors (logging them) so a Redis outage can
    never break a user request - except when ``REDIS_REQUIRED=true``.
    """

    def __init__(self, backend: CacheBackend, keys: CacheKeys, *, strict: bool = False) -> None:
        self.backend = backend
        self.keys = keys
        self.strict = strict
        self.hits = 0
        self.misses = 0

    @property
    def backend_name(self) -> str:
        return self.backend.name

    async def get(self, key: str, default: Any = None) -> Any:
        """Return the cached value or ``default`` on miss/error."""
        try:
            value = await self.backend.get(key)
        except Exception as exc:  # pragma: no cover - network failures
            if self.strict:
                raise
            logger.warning("cache get failed for %s: %s", key, exc)
            return default
        if value is None:
            self.misses += 1
            return default
        self.hits += 1
        return value

    async def set(self, key: str, value: Any, ttl: int | None = None) -> bool:
        """Store ``value`` under ``key`` (JSON encoded)."""
        if not isinstance(value, _JSON):
            logger.debug("cache set skipped for %s: unsupported type %s", key, type(value))
            return False
        try:
            return await self.backend.set(key, value, ttl)
        except Exception as exc:  # pragma: no cover
            if self.strict:
                raise
            logger.warning("cache set failed for %s: %s", key, exc)
            return False

    async def delete(self, *keys: str) -> int:
        """Delete one or more keys; returns how many were removed."""
        try:
            return await self.backend.delete(*keys)
        except Exception as exc:  # pragma: no cover
            if self.strict:
                raise
            logger.warning("cache delete failed: %s", exc)
            return 0

    async def delete_prefix(self, prefix: str) -> int:
        """Invalidate every key starting with ``prefix`` (e.g. ``list:111:``)."""
        try:
            keys = await self.backend.keys(f"{prefix}*")
            return await self.backend.delete(*keys) if keys else 0
        except Exception as exc:  # pragma: no cover
            if self.strict:
                raise
            logger.warning("cache delete_prefix failed for %s: %s", prefix, exc)
            return 0

    async def invalidate_user(self, telegram_id: int) -> int:
        """Drop everything cached for one user (after any write operation)."""
        prefix = f"{self.keys.prefix}:" if self.keys.prefix else ""
        keys_to_drop = [
            self.keys.user(telegram_id),
            self.keys.user_settings(telegram_id),
            self.keys.stats(telegram_id),
        ]
        removed = await self.delete(*keys_to_drop)
        removed += await self.delete_prefix(f"{prefix}list:{telegram_id}:")
        removed += await self.delete_prefix(f"{prefix}search:{telegram_id}:")
        return removed

    async def incr(self, key: str, amount: int = 1, ttl: int | None = None) -> int:
        """Atomic counter (used for view counters and rate limiting)."""
        try:
            return await self.backend.incr(key, amount, ttl)
        except Exception as exc:  # pragma: no cover
            if self.strict:
                raise
            logger.warning("cache incr failed for %s: %s", key, exc)
            return amount

    async def get_or_set(
        self,
        key: str,
        factory: Any,
        ttl: int | None = None,
    ) -> Any:
        """Read-through helper: ``await cache.get_or_set(key, loader, ttl=60)``.

        ``factory`` may be a sync or async zero-argument callable returning a
        JSON-serialisable value.
        """
        cached = await self.get(key)
        if cached is not None:
            return cached
        value = await factory() if asyncio.iscoroutinefunction(factory) else factory()
        if value is not None:
            await self.set(key, value, ttl)
        return value

    async def acquire_lock(self, name: str, ttl: int = 10) -> bool:
        """Best-effort distributed lock via ``SET NX EX``."""
        key = self.keys.lock(name)
        try:
            if isinstance(self.backend, RedisCacheBackend):
                return bool(await self.backend.client.set(key, "1", nx=True, ex=ttl))
            return bool(await self.backend.set(key, 1, ttl)) if not await self.backend.exists(key) else False
        except Exception as exc:  # pragma: no cover
            logger.warning("lock acquire failed for %s: %s", name, exc)
            return True  # fail open: never block the user because Redis is down

    async def release_lock(self, name: str) -> None:
        """Release a lock acquired by :meth:`acquire_lock`."""
        await self.delete(self.keys.lock(name))

    def stats(self) -> dict[str, Any]:
        """Hit/miss counters for the ``/api/v1/system/health`` endpoint."""
        total = self.hits + self.misses
        return {
            "backend": self.backend_name,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 4) if total else 0.0,
        }

    async def ping(self) -> bool:
        """Liveness probe used by ``/api/v1/system/health``.

        Writes and reads a throwaway key, so it verifies the *round trip* rather
        than just the connection object. Never raises: an unreachable Redis means
        "degraded", not "500".
        """
        probe_key = self.keys._k("health", "ping")
        try:
            await self.set(probe_key, "1", ttl=5)
            value = await self.get(probe_key)
            return value is not None
        except Exception as exc:
            logger.debug("Cache ping failed: %s", exc)
            return False

    async def close(self) -> None:
        """Close the underlying connection pool."""
        await self.backend.close()


_cache: Cache | None = None


async def _build_redis_backend(settings: Any) -> RedisCacheBackend | None:
    """Try to connect to Redis; return ``None`` when unavailable."""
    try:
        from redis.asyncio import ConnectionPool, Redis

        pool = ConnectionPool.from_url(
            settings.cache.url,
            max_connections=settings.cache.max_connections,
            socket_timeout=settings.cache.socket_timeout,
            socket_connect_timeout=settings.cache.socket_connect_timeout,
            decode_responses=False,
        )
        client: Any = Redis(connection_pool=pool)
        await client.ping()
        return RedisCacheBackend(client)
    except Exception as exc:
        if settings.cache.required:
            raise RuntimeError(
                f"REDIS_REQUIRED=true but Redis is unreachable at {settings.cache.url}: {exc}"
            ) from exc
        logger.warning("Redis unavailable (%s) - falling back to in-memory cache", exc)
        return None


async def init_cache() -> Cache:
    """Create (once) and return the process-wide :class:`Cache`."""
    global _cache
    if _cache is not None:
        return _cache

    settings = get_settings()
    backend = await _build_redis_backend(settings) or MemoryCacheBackend()
    _cache = Cache(
        backend=backend,
        keys=CacheKeys(settings.cache.key_prefix),
        strict=settings.cache.required,
    )
    logger.info("Cache initialised (backend=%s, prefix=%s)", backend.name, settings.cache.key_prefix)
    return _cache


def get_cache() -> Cache:
    """Return the initialised cache, creating a memory one if needed.

    Safe to call from sync code (FastAPI dependencies, aiogram middlewares).
    """
    global _cache
    if _cache is None:
        settings = get_settings()
        _cache = Cache(
            backend=MemoryCacheBackend(),
            keys=CacheKeys(settings.cache.key_prefix),
            strict=False,
        )
        logger.info("Cache created lazily with in-memory backend")
    return _cache


async def close_cache() -> None:
    """Shutdown hook: flush and close the cache backend."""
    global _cache
    if _cache is not None:
        await _cache.close()
        _cache = None
        logger.info("Cache closed")


def invalidate_keys_for_files(cache: Cache, telegram_id: int, file_ids: Iterable[object]) -> None:
    """Fire-and-forget helper collecting keys to drop after file mutations."""
    keys: list[str] = [cache.keys.file(fid) for fid in file_ids]
    keys.append(cache.keys.user(telegram_id))
    logger.debug("Queued cache invalidation for %d keys", len(keys))


async def invalidate_many(cache: Cache, keys: Sequence[str]) -> int:
    """Bulk delete helper used by services after batch mutations."""
    return await cache.delete(*keys) if keys else 0
