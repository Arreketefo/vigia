"""Aviasales (Travelpayouts Data API) flight source — Layer 1 breadth.

Cache data built from real user searches (~48h/7d retention): it is signal,
not live truth. Endpoints and field names verified against the live docs
(2026-07): `/aviasales/v3/grouped_prices` is the documented replacement for
`/v1/prices/calendar`, and `/aviasales/v3/search_by_price_range` supports
`destination=-` for open discovery. Entries carry `departure_at`/`return_at`
(ISO 8601) and a `link` relative to aviasales.com. Parsing stays defensive:
entries missing price/dates are skipped, never fatal.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import httpx
from radar_core.http import CircuitBreaker, get_json
from radar_core.ratelimit import TokenBucket

from vigia.contracts import FlightQuote

log = logging.getLogger(__name__)

_BASE = "https://api.travelpayouts.com"
_SITE = "https://www.aviasales.com"


class AviasalesFlightSource:
    name = "aviasales"

    def __init__(
        self,
        token: str,
        currency: str = "eur",
        market: str = "es",
        trip_min_nights: int = 2,
        trip_max_nights: int = 14,
        max_flight_hours: float | None = None,
    ) -> None:
        self._currency = currency
        self._market = market
        self._trip_min_nights = trip_min_nights
        self._trip_max_nights = trip_max_nights
        # The API's `duration` field is the ROUND-TRIP total in minutes
        # (verified empirically: ALC->LON direct reports 325 = 2 x ~165), so
        # a one-way bound of H hours becomes 2*H*60 total minutes.
        self._max_duration_min = int(max_flight_hours * 120) if max_flight_hours else None
        # Token travels in the X-Access-Token header (both auth modes are
        # documented) so request URLs — which httpx logs — never contain it.
        self._client = httpx.AsyncClient(
            timeout=15.0, headers={"X-Access-Token": token}
        )
        # Documented limit: 600 req/min for the v3 endpoints; stay conservative.
        self._bucket = TokenBucket(rate=4.0, capacity=8.0)
        self._breaker = CircuitBreaker()

    async def search_range(
        self, origin: str, month_bucket: str, price_min: float, price_max: float
    ) -> list[FlightQuote]:
        """Open discovery: cheapest round trips from origin to ANY destination.

        `destination` is OMITTED on purpose: the docs' historical 'destination=-'
        is rejected by current validation ("length must be exactly 3"); leaving
        it out returns all routes (verified live 2026-07-05).
        """
        params: dict[str, Any] = {
            "origin": origin,
            "value_min": max(int(price_min), 1),
            "value_max": int(price_max),
            "one_way": "false",
            "locale": "en",
            "currency": self._currency,
            "market": self._market,
            "limit": 100,
            "page": 1,
        }
        payload = await get_json(
            self._client, f"{_BASE}/aviasales/v3/search_by_price_range", params,
            self._bucket, self._breaker,
        )
        entries = payload.get("data", [])
        if not isinstance(entries, list):
            return []
        return [q for e in entries if (q := self._to_quote(e)) is not None]

    async def calendar(
        self, origin: str, destination: str, month_bucket: str
    ) -> list[FlightQuote]:
        """Cheapest round trip per departure day for one route over one month.

        return_at is pinned to the same month bucket, so stays crossing the
        month boundary are missed — acceptable for a signal radar.
        """
        params: dict[str, Any] = {
            "origin": origin,
            "destination": destination,
            "group_by": "departure_at",
            "departure_at": month_bucket,
            "return_at": month_bucket,
            "min_trip_duration": self._trip_min_nights,
            "max_trip_duration": self._trip_max_nights,
            "currency": self._currency,
            "market": self._market,
        }
        payload = await get_json(
            self._client, f"{_BASE}/aviasales/v3/grouped_prices", params,
            self._bucket, self._breaker,
        )
        data = payload.get("data", {})
        if not isinstance(data, dict):
            return []
        quotes = []
        for key, entry in data.items():
            quote = self._to_quote(entry, fallback_depart=key)
            if quote is not None:
                quotes.append(quote)
        return quotes

    def _to_quote(self, entry: Any, fallback_depart: str | None = None) -> FlightQuote | None:
        if not isinstance(entry, dict):
            return None
        depart = _parse_date(entry.get("departure_at") or fallback_depart)
        price = entry.get("price")
        if depart is None or price is None:
            return None
        if self._exceeds_duration(entry):
            return None  # flight longer than the configured bound
        ret = _parse_date(entry.get("return_at"))
        origin = str(entry.get("origin") or entry.get("origin_code") or "")
        destination = str(entry.get("destination") or entry.get("destination_code") or "")
        airline = entry.get("airline")
        return FlightQuote(
            origin=origin,
            destination=destination,
            depart_date=depart,
            return_date=ret,
            price=float(price),
            currency=self._currency,
            is_live=False,
            deep_link=_deep_link(entry.get("link"), origin, destination, depart, ret),
            source=self.name,
            airline=str(airline).upper() if airline else None,
            depart_time=_parse_time(entry.get("departure_at")),
            return_time=_parse_time(entry.get("return_at")),
        )

    def _exceeds_duration(self, entry: dict[str, Any]) -> bool:
        """True if the itinerary is longer than the configured one-way bound.

        Prefers the per-leg fields (duration_to/duration_back) when present;
        otherwise falls back to `duration` (round-trip total) vs 2x the bound,
        which lets asymmetric routings slip — best effort on cache data.
        """
        if self._max_duration_min is None:
            return False
        legs = [
            v for v in (entry.get("duration_to"), entry.get("duration_back"))
            if isinstance(v, int | float)
        ]
        if legs:
            return max(legs) > self._max_duration_min / 2
        total = entry.get("duration")
        return isinstance(total, int | float) and total > self._max_duration_min

    async def aclose(self) -> None:
        await self._client.aclose()


def _deep_link(
    link: Any, origin: str, dest: str, depart: date, ret: date | None
) -> str | None:
    if isinstance(link, str) and link:
        # grouped_prices links start with '/search/...'; search_by_price_range
        # links are relative to https://www.aviasales.com/search/ (no prefix).
        if link.startswith("http"):
            return link
        if link.startswith("/search/"):
            return f"{_SITE}{link}"
        return f"{_SITE}/search/{link.lstrip('/')}"
    if not origin or not dest:
        return None
    url = f"{_SITE}/search/{origin}{depart:%d%m}{dest}"
    if ret is not None:
        url += f"{ret:%d%m}"
    return url


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return datetime.fromisoformat(value[:10]).date()
    except ValueError:
        return None


def _parse_time(value: Any) -> str | None:
    """"HH:MM" local del ISO 8601 de la API ("2026-08-09T06:25:00+02:00").

    Los buckets date-only ("2026-08-09") no traen hora: None, y la línea de
    horario simplemente no se muestra en la alerta.
    """
    if not isinstance(value, str) or "T" not in value:
        return None
    try:
        return datetime.fromisoformat(value).strftime("%H:%M")
    except ValueError:
        return None
