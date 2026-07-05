"""Stable core interfaces. Providers implement these; the core never imports providers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol


@dataclass(frozen=True)
class FlightQuote:
    origin: str
    destination: str
    depart_date: date
    return_date: date | None
    price: float               # per person, EUR
    currency: str
    is_live: bool
    deep_link: str | None
    source: str


@dataclass(frozen=True)
class HotelQuote:
    location: str              # IATA or city id
    check_in: date
    check_out: date
    price_per_night: float     # total room, EUR
    currency: str
    is_live: bool
    deep_link: str | None
    source: str


@dataclass(frozen=True)
class Deal:
    origin: str
    destination: str
    depart_date: date
    return_date: date | None
    nights: int
    total_price: float
    baseline: float | None          # detection baseline (flight-only when enriched)
    drop_pct: float | None
    confirmed: bool
    dedup_key: str
    flight_link: str | None
    hotel_link: str | None
    # Set ONLY by candidate enrichment: total_price then includes
    # hotel_price_night * nights while baseline/drop_pct stay flight-only.
    hotel_price_night: float | None = None


class FlightSource(Protocol):
    name: str

    async def search_range(
        self, origin: str, month_bucket: str,
        price_min: float, price_max: float,
    ) -> Sequence[FlightQuote]: ...

    async def calendar(
        self, origin: str, destination: str, month_bucket: str,
    ) -> Sequence[FlightQuote]: ...


class HotelSource(Protocol):
    name: str

    async def cheapest(
        self, location: str, check_in: date, check_out: date,
    ) -> HotelQuote | None: ...


class PriceConfirmer(Protocol):
    """Layer 2. Re-price a candidate with live inventory (pay-per-use)."""

    name: str

    async def confirm(self, deal: Deal) -> Deal: ...   # sets confirmed=True + live prices


class Notifier(Protocol):
    channel: str

    async def send(self, deal: Deal) -> None: ...
