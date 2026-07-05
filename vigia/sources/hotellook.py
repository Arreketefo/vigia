"""Hotellook (Travelpayouts) hotel source — cheapest cached price per city + dates.

DEAD UPSTREAM: Travelpayouts shut the Hotellook API down on 2025-10-20; both
endpoints return 404 as of 2026-07. Kept behind the HotelSource interface
(disabled by default, see Settings.hotel_source) in case it is revived or a
compatible replacement appears. Shapes follow the archived docs: priceFrom is
the cheapest price for the WHOLE stay; lookup.json resolves IATA/city name ->
locationId (cached in memory for the daemon's lifetime).
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import httpx

from vigia.contracts import HotelQuote
from vigia.http import CircuitBreaker, get_json
from vigia.ratelimit import TokenBucket

log = logging.getLogger(__name__)

_BASE = "https://engine.hotellook.com/api/v2"
_SITE = "https://search.hotellook.com"


class HotellookHotelSource:
    name = "hotellook"

    def __init__(self, token: str, currency: str = "eur") -> None:
        self._token = token
        self._currency = currency
        self._client = httpx.AsyncClient(timeout=20.0)
        # Documented limit ~60 req/min; stay under it.
        self._bucket = TokenBucket(rate=0.8, capacity=5.0)
        self._breaker = CircuitBreaker()
        self._location_ids: dict[str, str | None] = {}

    async def cheapest(
        self, location: str, check_in: date, check_out: date
    ) -> HotelQuote | None:
        location_id = await self._location_id(location)
        if location_id is None:
            return None
        params: dict[str, Any] = {
            "locationId": location_id,
            "checkIn": check_in.isoformat(),
            "checkOut": check_out.isoformat(),
            "currency": self._currency,
            "limit": 10,
            "token": self._token,
        }
        payload = await get_json(
            self._client, f"{_BASE}/cache.json", params, self._bucket, self._breaker
        )
        if not isinstance(payload, list):
            return None
        nights = max((check_out - check_in).days, 1)
        prices = [
            float(entry["priceFrom"])
            for entry in payload
            if isinstance(entry, dict) and entry.get("priceFrom")
        ]
        if not prices:
            return None
        # priceFrom is the cheapest price for the WHOLE stay -> normalize per night.
        per_night = min(prices) / nights
        return HotelQuote(
            location=location,
            check_in=check_in,
            check_out=check_out,
            price_per_night=per_night,
            currency=self._currency,
            is_live=False,
            deep_link=(
                f"{_SITE}/?locationId={location_id}"
                f"&checkIn={check_in.isoformat()}&checkOut={check_out.isoformat()}"
            ),
            source=self.name,
        )

    async def _location_id(self, query: str) -> str | None:
        if query in self._location_ids:
            return self._location_ids[query]
        params: dict[str, Any] = {
            "query": query,
            "lang": "en",
            "lookFor": "city",
            "limit": 1,
            "token": self._token,
        }
        location_id: str | None = None
        try:
            payload = await get_json(
                self._client, f"{_BASE}/lookup.json", params, self._bucket, self._breaker
            )
            locations = payload.get("results", {}).get("locations", [])
            if locations:
                location_id = str(locations[0]["id"])
        except (httpx.HTTPError, KeyError, TypeError) as exc:
            log.warning("hotellook lookup failed for %r: %s", query, exc)
            return None  # do not cache transient failures
        if location_id is None:
            log.info("hotellook: no location found for %r", query)
        self._location_ids[query] = location_id
        return location_id

    async def aclose(self) -> None:
        await self._client.aclose()
