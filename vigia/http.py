"""HTTP helpers shared by sources: token bucket + exponential backoff with
jitter, plus a per-provider circuit breaker (§7) that stops hammering a host
that is persistently failing."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx

from vigia.ratelimit import TokenBucket

log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 4
_BASE_DELAY_S = 1.0


class CircuitOpenError(RuntimeError):
    """Provider is cooling down after persistent failures."""


class CircuitBreaker:
    """Opens after `threshold` consecutive failed requests (each already
    backoff-retried); while open, calls fail fast for `cooldown_s`."""

    def __init__(self, threshold: int = 3, cooldown_s: float = 600.0) -> None:
        self._threshold = threshold
        self._cooldown_s = cooldown_s
        self._failures = 0
        self._open_until = 0.0

    def ensure_closed(self) -> None:
        remaining = self._open_until - time.monotonic()
        if remaining > 0:
            raise CircuitOpenError(f"circuit open for another {remaining:.0f}s")

    def record_success(self) -> None:
        self._failures = 0

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._open_until = time.monotonic() + self._cooldown_s
            self._failures = 0
            log.warning("circuit breaker OPEN for %.0fs after %d consecutive failures",
                        self._cooldown_s, self._threshold)


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, Any],
    bucket: TokenBucket,
    breaker: CircuitBreaker | None = None,
) -> Any:
    """GET with rate limiting; retries 429/5xx/transport errors with backoff."""
    return await request_json(client, "GET", url, bucket, breaker, params=params)


async def post_json(
    client: httpx.AsyncClient,
    url: str,
    body: dict[str, Any],
    bucket: TokenBucket,
    breaker: CircuitBreaker | None = None,
) -> Any:
    """POST (JSON body) with the same rate limiting / backoff / breaker."""
    return await request_json(client, "POST", url, bucket, breaker, json_body=body)


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    bucket: TokenBucket,
    breaker: CircuitBreaker | None = None,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> Any:
    if breaker is not None:
        breaker.ensure_closed()
    try:
        result = await _request_with_backoff(client, method, url, params, json_body, bucket)
    except (httpx.TransportError, httpx.HTTPStatusError):
        if breaker is not None:
            breaker.record_failure()
        raise
    if breaker is not None:
        breaker.record_success()
    return result


async def _request_with_backoff(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    params: dict[str, Any] | None,
    json_body: dict[str, Any] | None,
    bucket: TokenBucket,
) -> Any:
    delay = _BASE_DELAY_S
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        await bucket.acquire()
        try:
            resp = await client.request(method, url, params=params, json=json_body)
        except httpx.TransportError as exc:
            if attempt == _MAX_ATTEMPTS:
                raise
            log.warning("%s %s transport error (%s), retry %d", method, url, exc, attempt)
        else:
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == _MAX_ATTEMPTS:
                    resp.raise_for_status()
                log.warning("%s %s -> %d, retry %d", method, url, resp.status_code, attempt)
            else:
                resp.raise_for_status()
                return resp.json()
        await asyncio.sleep(delay + random.uniform(0, delay * 0.3))
        delay *= 2
    raise RuntimeError("unreachable")  # pragma: no cover
