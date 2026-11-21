"""Token-bucket / sliding-window throttling for API endpoints and bot callbacks.

Two interchangeable implementations of the same interface:

* :class:`RedisRateLimiter`  - shared state across processes (production).
  Uses a Redis sorted set per (scope, identity) with timestamps as scores, so
  the window slides smoothly instead of resetting on the minute.
* :class:`MemoryRateLimiter` - per-process fallback for local development and
  tests, with the same semantics and O(1) amortised cost.

``check()`` never raises: it returns a :class:`RateLimitResult` so callers can
decide between answering with HTTP 429, editing a Telegram message, or silently
dropping an inline query.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from services.cache import Cache, RedisCacheBackend

logger = logging.getLogger(__name__)

__all__ = [
    "MemoryRateLimiter",
    "RateLimitResult",
    "RateLimiter",
    "RedisRateLimiter",
    "ThrottleExceeded",
    "build_rate_limiter",
    "get_rate_limiter",
]


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    """Outcome of a single throttling check."""

    allowed: bool
    limit: int
    remaining: int
    retry_after: int
    scope: str
    identity: str

    def as_headers(self) -> dict[str, str]:
        """Standard ``RateLimit-*`` / ``Retry-After`` response headers."""
        headers = {
            "RateLimit-Limit": str(self.limit),
            "RateLimit-Remaining": str(max(0, self.remaining)),
            "RateLimit-Reset": str(self.retry_after),
        }
        if not self.allowed:
            headers["Retry-After"] = str(max(1, self.retry_after))
        return headers

    def __str__(self) -> str:  # pragma: no cover - debug helper
        state = "ALLOW" if self.allowed else "DENY"
        return f"<{state} {self.scope}:{self.identity} {self.remaining}/{self.limit} retry={self.retry_after}s>"


class ThrottleExceeded(RuntimeError):
    """Raised by :meth:`RateLimiter.acquire` when the caller must back off."""

    def __init__(self, result: RateLimitResult) -> None:
        super().__init__(f"Rate limit exceeded for {result.scope}:{result.identity}")
        self.result = result
        self.retry_after = result.retry_after


class RateLimiter:
    """Common interface + shared helpers."""

    backend_name: str = "abstract"

    async def check(
        self, scope: str, identity: str, *, limit: int, window: int
    ) -> RateLimitResult:
        """Consume one unit and report whether the call is allowed."""
        raise NotImplementedError

    async def peek(self, scope: str, identity: str, *, limit: int, window: int) -> int:
        """Return the number of units already used (does not consume)."""
        raise NotImplementedError

    async def reset(self, scope: str, identity: str) -> None:
        """Clear the counter (used after a successful admin action)."""
        raise NotImplementedError

    async def acquire(
        self, scope: str, identity: str, *, limit: int, window: int
    ) -> RateLimitResult:
        """Like :meth:`check` but raises :class:`ThrottleExceeded` when denied."""
        result = await self.check(scope, identity, limit=limit, window=window)
        if not result.allowed:
            raise ThrottleExceeded(result)
        return result

    async def close(self) -> None:
        """Release resources (no-op for most backends)."""


class MemoryRateLimiter(RateLimiter):
    """Sliding window kept in a ``defaultdict[str, deque[float]]``."""

    backend_name = "memory"

    def __init__(self, *, max_buckets: int = 50_000) -> None:
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._max_buckets = max_buckets
        self._lock = asyncio.Lock()

    @staticmethod
    def _bucket(scope: str, identity: str) -> str:
        return f"{scope}:{identity}"

    def _prune(self, bucket: deque[float], cutoff: float) -> None:
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

    async def check(
        self, scope: str, identity: str, *, limit: int, window: int
    ) -> RateLimitResult:
        now = time.monotonic()
        cutoff = now - window
        key = self._bucket(scope, identity)
        async with self._lock:
            if len(self._buckets) > self._max_buckets:
                # Cheap GC: drop buckets that are already fully expired.
                for stale in [k for k, v in self._buckets.items() if not v or v[-1] <= cutoff]:
                    self._buckets.pop(stale, None)
            bucket = self._buckets[key]
            self._prune(bucket, cutoff)
            used = len(bucket)
            if used >= limit:
                retry_after = max(1, int(window - (now - bucket[0])) + 1)
                return RateLimitResult(False, limit, 0, retry_after, scope, identity)
            bucket.append(now)
            return RateLimitResult(True, limit, limit - used - 1, window, scope, identity)

    async def peek(self, scope: str, identity: str, *, limit: int, window: int) -> int:
        bucket = self._buckets.get(self._bucket(scope, identity))
        if not bucket:
            return 0
        self._prune(bucket, time.monotonic() - window)
        return len(bucket)

    async def reset(self, scope: str, identity: str) -> None:
        self._buckets.pop(self._bucket(scope, identity), None)

    async def close(self) -> None:
        self._buckets.clear()


class RedisRateLimiter(RateLimiter):
    """Sliding window on a Redis sorted set (``ZADD``/``ZREMRANGEBYSCORE``).

    The whole check runs in one round trip when the server supports pipelines,
    and degrades to sequential commands otherwise.
    """

    backend_name = "redis"

    def __init__(self, cache: Cache) -> None:
        self._cache = cache
        backend = cache.backend
        if not isinstance(backend, RedisCacheBackend):  # pragma: no cover - guarded by factory
            raise TypeError("RedisRateLimiter requires a RedisCacheBackend")
        self._client: Any = backend.client

    def _key(self, scope: str, identity: str) -> str:
        return self._cache.keys.rate(scope, identity)

    async def check(
        self, scope: str, identity: str, *, limit: int, window: int
    ) -> RateLimitResult:
        key = self._key(scope, identity)
        now = time.time()
        cutoff = now - window
        member = f"{now:.6f}:{id(asyncio.current_task())}"
        try:
            async with self._client.pipeline(transaction=True) as pipe:
                pipe.zremrangebyscore(key, 0, cutoff)
                pipe.zcard(key)
                pipe.zadd(key, {member: now})
                pipe.expire(key, window + 1)
                _, used_before, _, _ = await pipe.execute()
        except Exception as exc:  # pragma: no cover - network failure
            logger.warning("Redis rate limit failed (%s) - allowing request", exc)
            return RateLimitResult(True, limit, limit, window, scope, identity)

        used_before = int(used_before or 0)
        if used_before >= limit:
            # Over quota: remove the member we just added and compute the wait.
            oldest = await self._client.zrange(key, 0, 0, withscores=True)
            retry_after = window
            if oldest:
                retry_after = max(1, int(oldest[0][1] + window - now) + 1)
            await self._client.zrem(key, member)
            return RateLimitResult(False, limit, 0, retry_after, scope, identity)

        return RateLimitResult(True, limit, limit - used_before - 1, window, scope, identity)

    async def peek(self, scope: str, identity: str, *, limit: int, window: int) -> int:
        key = self._key(scope, identity)
        try:
            await self._client.zremrangebyscore(key, 0, time.time() - window)
            return int(await self._client.zcard(key))
        except Exception:  # pragma: no cover
            return 0

    async def reset(self, scope: str, identity: str) -> None:
        try:
            await self._client.delete(self._key(scope, identity))
        except Exception:  # pragma: no cover
            logger.debug("rate limit reset failed for %s:%s", scope, identity)


_limiter: RateLimiter | None = None


def build_rate_limiter(cache: Cache) -> RateLimiter:
    """Pick the best limiter for the current cache backend."""
    if isinstance(cache.backend, RedisCacheBackend):
        logger.info("Rate limiter: Redis sliding window")
        return RedisRateLimiter(cache)
    logger.info("Rate limiter: in-memory sliding window (Redis unavailable)")
    return MemoryRateLimiter()


async def init_rate_limiter(cache: Cache | None = None) -> RateLimiter:
    """Create the process-wide limiter (call after :func:`init_cache`)."""
    global _limiter
    from services.cache import get_cache

    _limiter = build_rate_limiter(cache or get_cache())
    return _limiter


def get_rate_limiter() -> RateLimiter:
    """Return the shared limiter, falling back to memory if not initialised."""
    global _limiter
    if _limiter is None:
        _limiter = MemoryRateLimiter()
    return _limiter


async def close_rate_limiter() -> None:
    """Shutdown hook."""
    global _limiter
    if _limiter is not None:
        await _limiter.close()
        _limiter = None
