"""Fixed-window rate limiting, with a backend you can share.

The original was an in-process dict, which is fine for one process and
wrong the moment there are two: with `--workers 4` behind three Fargate
tasks, "8 submissions per 10 minutes" silently became 96. That is not a
tuning problem, it is the limiter not working.

Backends, chosen with `RATE_LIMIT_BACKEND`:

  memory     the original dict. Correct only in a single process, so
             it is the development default and nothing more.
  postgres   one row per key per window in an UNLOGGED table,
             incremented atomically. Shared across every replica with
             no new infrastructure, which at this size is the right
             trade — the writes are small and the table is tiny.
  redis      for when the write volume outgrows Postgres. Same
             semantics, one round trip.

`check()` is async because two of the three backends do I/O. A backend
failure **allows** the request: a rate limiter that returns 500 when
its store is briefly unreachable has turned a throttle into an outage.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from fastapi import HTTPException

from .config import settings

log = logging.getLogger("crm.ratelimit")


@dataclass(slots=True)
class _Bucket:
    count: int
    reset_at: float


class MemoryBackend:
    """Per-process counters. Correct in one process only."""

    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}

    def _sweep(self, now: float) -> None:
        # Cheap amortised cleanup so the dict cannot grow without bound.
        if len(self._buckets) < 512:
            return
        for key in [k for k, b in self._buckets.items() if b.reset_at <= now]:
            self._buckets.pop(key, None)

    async def hit(self, key: str, limit: int, window: float) -> tuple[int, int]:
        """Returns (count after this hit, seconds until the window resets)."""
        now = time.monotonic()
        self._sweep(now)
        bucket = self._buckets.get(key)

        if bucket is None or bucket.reset_at <= now:
            self._buckets[key] = _Bucket(count=1, reset_at=now + window)
            return 1, int(window)

        bucket.count += 1
        return bucket.count, max(1, int(bucket.reset_at - now))

    async def reset(self, key: str) -> None:
        self._buckets.pop(key, None)


class PostgresBackend:
    """Shared counters in an UNLOGGED table.

    The window is derived from the clock rather than stored, so the
    upsert is a single statement with no read-modify-write race: two
    replicas hitting the same key in the same window both increment the
    same row, and the returned value is authoritative.
    """

    async def hit(self, key: str, limit: int, window: float) -> tuple[int, int]:
        from . import db  # noqa: PLC0415 — avoids a cycle at import

        now = int(time.time())
        window_seconds = max(1, int(window))
        window_start = now - (now % window_seconds)
        resets_in = window_start + window_seconds - now

        try:
            row = await db.fetch_one(
                """INSERT INTO rate_limits (bucket, window_start, hits, expires_at)
                   VALUES ($1, $2, 1, to_timestamp($3))
                   ON CONFLICT (bucket, window_start) DO UPDATE
                      SET hits = rate_limits.hits + 1
                   RETURNING hits""",
                key, window_start, window_start + window_seconds * 2,
            )
        except Exception as exc:
            # Fail open. A limiter that 500s when its store blinks has
            # turned a throttle into an outage.
            log.error("rate limit store unavailable, allowing request: %s", exc)
            return 0, resets_in

        return int((row or {}).get("hits", 1)), max(1, resets_in)

    async def reset(self, key: str) -> None:
        from . import db  # noqa: PLC0415

        try:
            await db.execute("DELETE FROM rate_limits WHERE bucket = $1", key)
        except Exception as exc:
            log.error("rate limit reset failed: %s", exc)


class RedisBackend:
    """INCR + EXPIRE. One round trip, and the obvious choice once the
    write volume makes Postgres the wrong place for this."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._client = None

    async def _redis(self):
        if self._client is None:
            try:
                import redis.asyncio as redis  # noqa: PLC0415
            except ImportError as exc:
                raise RuntimeError(
                    "RATE_LIMIT_BACKEND=redis needs the redis package. "
                    "Add redis>=5 to requirements.txt."
                ) from exc
            self._client = redis.from_url(self._url, decode_responses=True)
        return self._client

    async def hit(self, key: str, limit: int, window: float) -> tuple[int, int]:
        window_seconds = max(1, int(window))
        now = int(time.time())
        window_start = now - (now % window_seconds)
        resets_in = window_start + window_seconds - now
        redis_key = f"rl:{key}:{window_start}"

        try:
            client = await self._redis()
            pipe = client.pipeline()
            pipe.incr(redis_key)
            pipe.expire(redis_key, window_seconds * 2)
            count, _ = await pipe.execute()
        except Exception as exc:
            log.error("redis rate limit unavailable, allowing request: %s", exc)
            return 0, resets_in
        return int(count), max(1, resets_in)

    async def reset(self, key: str) -> None:
        try:
            client = await self._redis()
            async for found in client.scan_iter(f"rl:{key}:*"):
                await client.delete(found)
        except Exception as exc:
            log.error("redis rate limit reset failed: %s", exc)


_backend = None


def backend():
    """The configured backend, built once."""
    global _backend
    if _backend is None:
        choice = (settings.rate_limit_backend or "memory").lower()
        if choice == "postgres":
            _backend = PostgresBackend()
        elif choice == "redis":
            _backend = RedisBackend(settings.redis_url)
        else:
            if choice != "memory":
                log.warning("unknown RATE_LIMIT_BACKEND %r; using memory", choice)
            _backend = MemoryBackend()
        log.info("rate limiting backend: %s", choice)
    return _backend


def describe() -> dict:
    """Reported on /healthz, so an install running several replicas on
    the memory backend can see that its limits are not shared."""
    choice = (settings.rate_limit_backend or "memory").lower()
    return {
        "backend": choice,
        "shared": choice in {"postgres", "redis"},
        "warning": (
            None if choice in {"postgres", "redis"}
            else "In-process counters: with more than one worker or task the "
                 "effective limit is multiplied by the process count."
        ),
    }


class RateLimiter:
    """One named budget. `name` scopes the key so two limiters with the
    same subject (an IP) do not share a counter."""

    __slots__ = ("max_requests", "window", "name")

    def __init__(self, max_requests: int, window_seconds: float, name: str = "rl") -> None:
        self.max_requests = max_requests
        self.window = window_seconds
        self.name = name

    async def check(self, key: str) -> None:
        """Raise 429 when the caller is over budget."""
        count, resets_in = await backend().hit(
            f"{self.name}:{key}", self.max_requests, self.window
        )
        # count == 0 means the backend failed and chose to allow.
        if count and count > self.max_requests:
            raise HTTPException(
                429,
                f"Too many attempts. Try again in {resets_in}s.",
                headers={
                    "retry-after": str(resets_in),
                    "x-ratelimit-limit": str(self.max_requests),
                    "x-ratelimit-remaining": "0",
                },
            )

    async def reset(self, key: str) -> None:
        """Clear one subject's budget — used by tests and by an admin
        unblocking someone who locked themselves out."""
        await backend().reset(f"{self.name}:{key}")


async def prune() -> int:
    """Drop expired windows. Only the Postgres backend accumulates."""
    if (settings.rate_limit_backend or "memory").lower() != "postgres":
        return 0
    from . import db  # noqa: PLC0415

    rows = await db.fetch(
        "DELETE FROM rate_limits WHERE expires_at < now() RETURNING bucket"
    )
    return len(rows)


login_limiter = RateLimiter(max_requests=10, window_seconds=600, name="login")
intake_limiter = RateLimiter(max_requests=8, window_seconds=600, name="intake")
# Each signup creates a tenant; keep the budget tight per IP.
signup_limiter = RateLimiter(max_requests=3, window_seconds=3600, name="signup")
# Reset requests send email; a loose limit here is a spam cannon.
reset_limiter = RateLimiter(max_requests=5, window_seconds=3600, name="reset")
