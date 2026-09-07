"""Fixed-window rate limiter, in-process.

NOTE: state lives in this worker process. Behind several uvicorn workers
or several Fargate tasks the effective limit multiplies by the process
count. Swap the dict for ElastiCache or an AWS WAF rate-based rule
before scaling out.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from fastapi import HTTPException


@dataclass(slots=True)
class _Bucket:
    count: int
    reset_at: float


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self.max_requests = max_requests
        self.window = window_seconds
        self._buckets: dict[str, _Bucket] = {}

    def _sweep(self, now: float) -> None:
        # Cheap amortised cleanup so the dict cannot grow without bound.
        if len(self._buckets) < 512:
            return
        for key in [k for k, b in self._buckets.items() if b.reset_at <= now]:
            self._buckets.pop(key, None)

    def check(self, key: str) -> None:
        """Raise 429 when the caller is over budget."""
        now = time.monotonic()
        self._sweep(now)
        bucket = self._buckets.get(key)

        if bucket is None or bucket.reset_at <= now:
            self._buckets[key] = _Bucket(count=1, reset_at=now + self.window)
            return

        if bucket.count >= self.max_requests:
            retry_in = int(bucket.reset_at - now) + 1
            raise HTTPException(
                429,
                f"Too many attempts. Try again in {retry_in}s.",
                headers={"retry-after": str(retry_in)},
            )

        bucket.count += 1


login_limiter = RateLimiter(max_requests=10, window_seconds=600)
intake_limiter = RateLimiter(max_requests=8, window_seconds=600)
# Each signup creates a tenant; keep the budget tight per IP.
signup_limiter = RateLimiter(max_requests=3, window_seconds=3600)
