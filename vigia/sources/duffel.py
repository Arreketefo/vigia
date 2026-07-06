"""Duffel Layer-2 price confirmer — re-prices a candidate's FLIGHT against
live bookable inventory just before alerting.

Pay-per-use by design (excess-search fee past a 1500:1 look-to-book ratio),
so it must only ever run on deals that already fired and passed dedup — the
scheduler guarantees that. Only the flight component is re-priced: the hotel
part added by candidate enrichment (hotel_price_night) is preserved. If
Duffel has no offers for the route/dates, the deal goes out unconfirmed —
missing live coverage must not silence the signal.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

import httpx
from radar_core.http import CircuitBreaker, post_json
from radar_core.ratelimit import TokenBucket

from vigia.contracts import Deal

log = logging.getLogger(__name__)

# supplier_timeout: don't wait for slow airlines; candidates are re-checked
# next tick anyway if they survive.
_URL = "https://api.duffel.com/air/offer_requests?return_offers=true&supplier_timeout=15000"


class DuffelPriceConfirmer:
    name = "duffel"

    def __init__(self, token: str, currency: str = "eur", pax: int = 2) -> None:
        self._currency = currency.upper()
        self._pax = pax
        self._client = httpx.AsyncClient(
            timeout=45.0,
            headers={
                "Authorization": f"Bearer {token}",
                "Duffel-Version": "v2",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        # Offer requests are heavyweight searches; keep the pace gentle.
        self._bucket = TokenBucket(rate=0.5, capacity=2.0)
        self._breaker = CircuitBreaker()

    async def confirm(self, deal: Deal) -> Deal:
        if deal.return_date is None:
            return deal
        body: dict[str, Any] = {
            "data": {
                "slices": [
                    {
                        "origin": deal.origin,
                        "destination": deal.destination,
                        "departure_date": deal.depart_date.isoformat(),
                    },
                    {
                        "origin": deal.destination,
                        "destination": deal.origin,
                        "departure_date": deal.return_date.isoformat(),
                    },
                ],
                "passengers": [{"type": "adult"}] * self._pax,
                "cabin_class": "economy",
            }
        }
        payload = await post_json(self._client, _URL, body, self._bucket, self._breaker)
        winner = _cheapest_offer(payload, self._currency)
        if winner is None:
            log.info(
                "duffel: no live %s offers for %s->%s %s; alerting unconfirmed",
                self._currency, deal.origin, deal.destination, deal.depart_date,
            )
            return deal
        flight_total, airline, depart_time, return_time = winner
        hotel_part = (deal.hotel_price_night or 0.0) * deal.nights
        # Keep the alert internally consistent: the displayed drop must refer
        # to the LIVE flight price, not to the cache price that triggered it.
        drop_pct = deal.drop_pct
        if deal.baseline:
            drop_pct = (deal.baseline - flight_total) / deal.baseline
        # Same consistency rule for the flight-detail line: the confirmed
        # price belongs to Duffel's winning offer, whose carrier/times may
        # differ from the cached quote that fired the deal — never show the
        # stale ones next to the live price.
        return replace(
            deal,
            total_price=flight_total + hotel_part,
            drop_pct=drop_pct,
            confirmed=True,
            airline=airline,
            depart_time=depart_time,
            return_time=return_time,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _cheapest_offer(
    payload: Any, currency: str
) -> tuple[float, str | None, str | None, str | None] | None:
    """(total, airline IATA, HH:MM ida, HH:MM vuelta) of the cheapest
    offers[].total_amount (all passengers) in the requested currency."""
    if not isinstance(payload, dict):
        return None
    offers = (payload.get("data") or {}).get("offers") or []
    best: tuple[float, str | None, str | None, str | None] | None = None
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        if str(offer.get("total_currency") or "").upper() != currency:
            continue
        amount = offer.get("total_amount")
        if amount is None:
            continue
        try:
            total = float(amount)
        except (TypeError, ValueError):
            continue
        if best is None or total < best[0]:
            airline = _offer_airline(offer)
            times = _slice_departure_times(offer)
            best = (total, airline, times[0], times[1])
    return best


def _offer_airline(offer: dict[str, Any]) -> str | None:
    owner = offer.get("owner")
    if isinstance(owner, dict) and owner.get("iata_code"):
        return str(owner["iata_code"]).upper()
    return None


def _slice_departure_times(offer: dict[str, Any]) -> tuple[str | None, str | None]:
    """HH:MM local de salida del primer segmento de cada slice (ida, vuelta)."""
    times: list[str | None] = [None, None]
    slices = offer.get("slices")
    if not isinstance(slices, list):
        return (None, None)
    for i, sl in enumerate(slices[:2]):
        if not isinstance(sl, dict):
            continue
        segments = sl.get("segments")
        if not isinstance(segments, list) or not segments:
            continue
        first = segments[0]
        departing = first.get("departing_at") if isinstance(first, dict) else None
        if isinstance(departing, str) and len(departing) >= 16 and departing[10] == "T":
            times[i] = departing[11:16]
    return (times[0], times[1])
