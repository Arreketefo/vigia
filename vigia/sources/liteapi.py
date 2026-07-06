"""LiteAPI (Nuitee) hotel source — live cheapest rates per city + dates.

Verified against the live API (2026-07-05): POST /v3.0/hotels/rates with
X-API-Key auth; `iataCode` resolves airport codes (BUD, PRG) but NOT metro
city codes (LON returns 0 hotels), hence the metro->city fallback map.
`offerRetailRate.amount` is the price for the WHOLE stay. Core endpoints are
free; intended usage here is candidate enrichment (a handful of calls/day),
not the Layer-1 sweep — LiteAPI publishes no look-to-book policy.
"""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any

import httpx
from radar_core.http import CircuitBreaker, post_json
from radar_core.ratelimit import TokenBucket

from vigia.contracts import HotelQuote

log = logging.getLogger(__name__)

_URL = "https://api.liteapi.travel/v3.0/hotels/rates"

# Hotel prices move slowly; caching (hits AND misses) bounds API spend when
# the same trip keeps re-firing — e.g. a candidate repeatedly killed by
# trip_budget_cap, or a notifier outage — to one POST per trip per TTL.
_CACHE_TTL_S = 6 * 3600.0
_CACHE_MAX_ENTRIES = 1024

# IATA metro-area codes that LiteAPI's iataCode param cannot resolve
# (it only knows airports). Extend as discovery surfaces more metro codes.
_METRO_CITIES: dict[str, tuple[str, str]] = {
    "LON": ("London", "GB"),
    "PAR": ("Paris", "FR"),
    "MIL": ("Milan", "IT"),
    "ROM": ("Rome", "IT"),
    "STO": ("Stockholm", "SE"),
    "MOW": ("Moscow", "RU"),
}


class LiteApiHotelSource:
    name = "liteapi"

    def __init__(
        self,
        api_key: str,
        currency: str = "eur",
        adults: int = 2,
        guest_nationality: str = "ES",
    ) -> None:
        self._currency = currency.upper()
        self._adults = adults
        self._guest_nationality = guest_nationality
        self._client = httpx.AsyncClient(
            timeout=20.0, headers={"X-API-Key": api_key}
        )
        # Candidate-enrichment volume is tiny; keep the bucket modest anyway.
        self._bucket = TokenBucket(rate=2.0, capacity=4.0)
        self._breaker = CircuitBreaker()
        # location -> body fragment that worked last time (skip failed probes)
        self._resolved: dict[str, dict[str, str]] = {}
        # (location, checkin, checkout) -> (expires_at_monotonic, stay_total)
        self._quote_cache: dict[tuple[str, str, str], tuple[float, float | None]] = {}

    async def cheapest(
        self, location: str, check_in: date, check_out: date
    ) -> HotelQuote | None:
        nights = (check_out - check_in).days
        if nights < 1:
            return None
        cache_key = (location, check_in.isoformat(), check_out.isoformat())
        cached = self._quote_cache.get(cache_key)
        if cached is not None and cached[0] > time.monotonic():
            stay_total = cached[1]
        else:
            stay_total = await self._cheapest_stay_total(location, check_in, check_out)
            self._store_in_cache(cache_key, stay_total)
        if stay_total is None:
            return None
        return HotelQuote(
            location=location,
            check_in=check_in,
            check_out=check_out,
            price_per_night=stay_total / nights,
            currency=self._currency.lower(),
            is_live=True,
            deep_link=self._deep_link(location, check_in, check_out),
            source=self.name,
        )

    async def _cheapest_stay_total(
        self, location: str, check_in: date, check_out: date
    ) -> float | None:
        for locator in self._locators(location):
            body: dict[str, Any] = {
                **locator,
                "checkin": check_in.isoformat(),
                "checkout": check_out.isoformat(),
                "occupancies": [{"adults": self._adults}],
                "currency": self._currency,
                "guestNationality": self._guest_nationality,
                "maxRatesPerHotel": 1,
                # The hotel list is NOT price-ordered (verified empirically):
                # a small limit would give the cheapest of an arbitrary
                # subset. One large page per trip is still a single POST.
                "limit": 1000,
                "timeout": 12,
            }
            payload = await post_json(self._client, _URL, body, self._bucket, self._breaker)
            total = _min_stay_total(payload, self._currency)
            if total is not None:
                self._resolved[location] = locator
                return total
        log.info("liteapi: no rates found for %r %s..%s", location, check_in, check_out)
        return None

    def _store_in_cache(self, key: tuple[str, str, str], stay_total: float | None) -> None:
        if len(self._quote_cache) >= _CACHE_MAX_ENTRIES:
            now = time.monotonic()
            self._quote_cache = {
                k: v for k, v in self._quote_cache.items() if v[0] > now
            }
        self._quote_cache[key] = (time.monotonic() + _CACHE_TTL_S, stay_total)

    def _locators(self, location: str) -> list[dict[str, str]]:
        """Location strategies to try in order; a previously successful one wins."""
        cached = self._resolved.get(location)
        if cached is not None:
            return [cached]
        locators: list[dict[str, str]] = [{"iataCode": location}]
        metro = _METRO_CITIES.get(location.upper())
        if metro is not None:
            locators.append({"cityName": metro[0], "countryCode": metro[1]})
        return locators

    def _deep_link(self, location: str, check_in: date, check_out: date) -> str:
        # Best-effort actionable link (LiteAPI itself has no public search UI).
        metro = _METRO_CITIES.get(location.upper())
        query = metro[0] if metro else location
        return (
            "https://www.booking.com/searchresults.html"
            f"?ss={query}&checkin={check_in.isoformat()}"
            f"&checkout={check_out.isoformat()}&group_adults={self._adults}"
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _min_stay_total(payload: Any, currency: str) -> float | None:
    """Cheapest whole-stay price across data[].roomTypes[].offerRetailRate.

    Offers whose currency differs from the requested one are skipped — a
    supplier-native amount (e.g. HUF) min()'d as EUR would be nonsense.
    """
    if not isinstance(payload, dict):
        return None
    totals: list[float] = []
    for hotel in payload.get("data") or []:
        if not isinstance(hotel, dict):
            continue
        for room_type in hotel.get("roomTypes") or []:
            if not isinstance(room_type, dict):
                continue
            rate = room_type.get("offerRetailRate") or {}
            amount = rate.get("amount")
            offer_currency = str(rate.get("currency") or currency).upper()
            if amount and offer_currency == currency.upper():
                totals.append(float(amount))
    return min(totals) if totals else None
