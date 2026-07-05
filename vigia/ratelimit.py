"""Per-provider async token bucket. Providers acquire() before every request."""

from __future__ import annotations

import asyncio
import time


class TokenBucket:
    """Allows `rate` acquisitions per second with bursts up to `capacity`."""

    def __init__(self, rate: float, capacity: float) -> None:
        if rate <= 0 or capacity <= 0:
            raise ValueError("rate and capacity must be positive")
        self._rate = rate
        self._capacity = capacity
        self._tokens = capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated) * self._rate
                )
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                wait = (tokens - self._tokens) / self._rate
            # Sleep OUTSIDE the lock so concurrent acquirers can drain the
            # burst capacity instead of serializing behind one sleeper.
            await asyncio.sleep(wait)
