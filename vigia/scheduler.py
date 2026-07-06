"""Orchestrator: one tick scans the stalest (route, month) pairs, records
observations, evaluates deals and fans out alerts. Rate limiting lives inside
each source (token bucket per provider).

`hotels` (Layer-1 sweep source) may be None: detection then runs on
flight-only totals (total = flight * pax). Independently, `enricher` is a
HotelSource consulted ONLY for deals that already fired and passed dedup
("candidates" mode): it prices the cheapest hotel, the trip total is checked
against trip_budget_cap, and the alert carries flight+hotel. This keeps hotel
API volume at a handful of calls per day.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from vigia import engine
from vigia.cities import CityDirectory
from vigia.config import Settings
from vigia.contracts import (
    Deal,
    FlightQuote,
    FlightSource,
    HotelQuote,
    HotelSource,
    Notifier,
    PriceConfirmer,
)
from vigia.store import PriceStore, Route, utcnow_str
from vigia.tripwindows import TripWindowPolicy

log = logging.getLogger(__name__)

LAST_TICK_KEY = "last_tick_at"

# Observations older than this feed no baseline (30-day window + margin).
_OBS_RETENTION_DAYS = 45


@dataclass
class TickStats:
    pairs_scanned: int = 0
    observations: int = 0
    deals_fired: int = 0
    alerts_sent: int = 0
    errors: int = 0
    discovered_routes: list[str] = field(default_factory=list)


def month_buckets(today: date, window_days: int) -> list[str]:
    """Rolling window today .. today+window_days as 'YYYY-MM' buckets."""
    buckets: list[str] = []
    current = today.replace(day=1)
    end = today + timedelta(days=window_days)
    while current <= end:
        buckets.append(current.strftime("%Y-%m"))
        current = (current + timedelta(days=32)).replace(day=1)
    return buckets


async def tick(
    flights: FlightSource,
    hotels: HotelSource | None,
    store: PriceStore,
    cfg: Settings,
    notifiers: list[Notifier],
    confirmer: PriceConfirmer | None = None,
    enricher: HotelSource | None = None,
    cities: CityDirectory | None = None,
    trip_policy: TripWindowPolicy | None = None,
) -> TickStats:
    stats = TickStats()
    today = date.today()
    buckets = month_buckets(today, cfg.window_days)
    if cfg.discovery:
        await _discover_routes(flights, store, cfg, stats, buckets[0], cities)

    excluded = await _excluded_destinations(store, cfg, cities)
    batch = await store.stalest_route_month_pairs(
        buckets, cfg.batch_size, exclude_destinations=excluded
    )
    for route, bucket in batch:
        # Mark the ATTEMPT before scanning so pairs that error or come back
        # empty still rotate to the back of the round-robin queue.
        await store.mark_scanned(route.id, bucket)
        try:
            await _scan_pair(route, bucket, today, flights, hotels, store, cfg,
                             notifiers, confirmer, enricher, trip_policy, stats)
        except Exception:
            stats.errors += 1
            log.exception("scan failed for %s->%s %s", route.origin, route.destination, bucket)
        stats.pairs_scanned += 1

    pruned = await store.prune_observations(_OBS_RETENTION_DAYS)
    if pruned:
        log.info("pruned %d observations older than %d days", pruned, _OBS_RETENTION_DAYS)
    await store.set_meta(LAST_TICK_KEY, utcnow_str())
    log.info(
        "tick done: %d pairs, %d observations, %d deals, %d alerts, %d errors",
        stats.pairs_scanned, stats.observations, stats.deals_fired,
        stats.alerts_sent, stats.errors,
    )
    return stats


async def _excluded_destinations(
    store: PriceStore, cfg: Settings, cities: CityDirectory | None
) -> set[str]:
    """Destinations of enabled routes whose country is excluded by config."""
    countries = cfg.excluded_countries()
    if not countries or cities is None:
        return set()
    excluded: set[str] = set()
    for route in await store.enabled_routes():
        if await cities.country(route.destination) in countries:
            excluded.add(route.destination)
    return excluded


async def _discover_routes(
    flights: FlightSource,
    store: PriceStore,
    cfg: Settings,
    stats: TickStats,
    bucket: str,
    cities: CityDirectory | None,
) -> None:
    """Open discovery: cheap round trips from origin to ANY destination under
    budget become watched routes. One request per tick; failure never kills a tick."""
    # Aviasales prices are per person; the budget cap is for the whole trip.
    per_person_cap = cfg.budget_cap / cfg.pax
    try:
        quotes = await flights.search_range(cfg.origin, bucket, 0, per_person_cap)
    except Exception as exc:
        log.warning("route discovery failed: %s", exc)
        return
    known = {r.destination for r in await store.enabled_routes()}
    countries = cfg.excluded_countries()
    for quote in quotes:
        dest = quote.destination.upper()
        if not dest or dest == cfg.origin.upper() or dest in known:
            continue
        if countries and cities is not None and await cities.country(dest) in countries:
            continue  # excluded country: don't even watch the route
        await store.upsert_route(cfg.origin, dest)
        known.add(dest)
        stats.discovered_routes.append(dest)
    if stats.discovered_routes:
        log.info("discovered %d new routes: %s",
                 len(stats.discovered_routes), ", ".join(stats.discovered_routes))


def _schedule_ok(
    depart_time: str | None, return_time: str | None,
    depart_after: str, return_before: str,
) -> bool:
    """Filtro OPCIONAL de horario ("" = apagado). Las horas desconocidas
    (buckets date-only de la caché) PASAN, igual que hace max_flight_hours
    con duraciones ausentes: no se pierden ofertas por dato ausente. Las
    "HH:MM" normalizadas comparan bien como strings."""
    depart_ok = not depart_after or not depart_time or depart_time >= depart_after
    return_ok = not return_before or not return_time or return_time <= return_before
    return depart_ok and return_ok


@dataclass(frozen=True)
class _Candidate:
    quote: FlightQuote
    hotel: HotelQuote | None
    nights: int
    total: float


async def _scan_pair(
    route: Route,
    bucket: str,
    today: date,
    flights: FlightSource,
    hotels: HotelSource | None,
    store: PriceStore,
    cfg: Settings,
    notifiers: list[Notifier],
    confirmer: PriceConfirmer | None,
    enricher: HotelSource | None,
    trip_policy: TripWindowPolicy | None,
    stats: TickStats,
) -> None:
    quotes = await flights.calendar(route.origin, route.destination, bucket)
    usable = [
        q for q in quotes
        if q.return_date is not None
        and q.return_date > q.depart_date
        and q.depart_date >= today  # cache can still hold departed trips
        and q.price > 0
        and (trip_policy is None or trip_policy.allows(q.depart_date, q.return_date))
        and _schedule_ok(q.depart_time, q.return_time, cfg.depart_after, cfg.return_before)
    ]
    # Hotel lookups are the scarce resource: only the cheapest flight-days of
    # the month get one. Flight-only mode keeps the same cap for symmetry.
    usable.sort(key=lambda q: q.price)

    candidates: list[_Candidate] = []
    for quote in usable[: cfg.max_quotes_per_pair]:
        assert quote.return_date is not None
        hotel: HotelQuote | None = None
        if hotels is not None:
            try:
                hotel = await hotels.cheapest(
                    route.destination, quote.depart_date, quote.return_date
                )
            except Exception:
                stats.errors += 1
                log.exception("hotel lookup failed for %s %s", route.destination,
                              quote.depart_date)
                continue
            if hotel is None:
                # A hotel source is configured but has no data: skip rather
                # than mixing flight-only totals into a flight+hotel baseline.
                continue
        nights = (quote.return_date - quote.depart_date).days
        total = quote.price * cfg.pax
        if hotel is not None:
            total += hotel.price_per_night * nights
        candidates.append(_Candidate(quote, hotel, nights, total))

    if not candidates:
        return

    # One transaction for the pair's observations, one baseline recompute for
    # all its candidates (they share (route, month) by construction).
    for c in candidates:
        await store.record_observation(
            route_id=route.id,
            depart_date=c.quote.depart_date,
            return_date=c.quote.return_date,
            nights=c.nights,
            flight_price=c.quote.price,
            hotel_price_night=c.hotel.price_per_night if c.hotel else None,
            source=c.quote.source,
            deep_link=c.quote.deep_link,
            is_live=c.quote.is_live,
            commit=False,
        )
    await store.commit()
    stats.observations += len(candidates)

    median, mad, sample = await store.baseline(
        route.id, bucket, cfg.pax, with_hotel=hotels is not None
    )
    for c in candidates:
        try:
            await _maybe_alert(route, c, median, mad, sample, store, cfg,
                               notifiers, confirmer, enricher, stats)
        except Exception:
            stats.errors += 1
            log.exception("deal evaluation failed for %s->%s %s",
                          route.origin, route.destination, c.quote.depart_date)


async def _maybe_alert(
    route: Route,
    c: _Candidate,
    median: float | None,
    mad: float | None,
    sample: int,
    store: PriceStore,
    cfg: Settings,
    notifiers: list[Notifier],
    confirmer: PriceConfirmer | None,
    enricher: HotelSource | None,
    stats: TickStats,
) -> None:
    fire, drop_pct = engine.is_deal(c.total, median, mad, sample, cfg)
    if not fire:
        return
    stats.deals_fired += 1

    if not await store.should_alert(
        route.id, c.quote.depart_date, c.quote.return_date, c.total, cfg.realert_drop
    ):
        log.debug("deal suppressed by dedup: %s->%s %s %.0f EUR",
                  route.origin, route.destination, c.quote.depart_date, c.total)
        return

    key = engine.dedup_key(
        route.origin, route.destination, c.quote.depart_date, c.quote.return_date, c.total
    )
    # A median over fewer than min_sample observations is noise: the deal
    # fired via hard_steal anyway, so don't show a meaningless baseline/drop.
    baseline_ready = sample >= cfg.min_sample
    deal = Deal(
        origin=route.origin,
        destination=route.destination,
        depart_date=c.quote.depart_date,
        return_date=c.quote.return_date,
        nights=c.nights,
        total_price=c.total,
        baseline=median if baseline_ready else None,
        drop_pct=drop_pct if baseline_ready else None,
        confirmed=False,
        dedup_key=key,
        flight_link=c.quote.deep_link,
        # hotel_price_night stays None here even in sweep mode: it marks
        # candidate ENRICHMENT, i.e. "baseline is flight-only, hotel added on
        # top" — sweep deals have hotel already inside total AND baseline.
        hotel_link=c.hotel.deep_link if c.hotel else None,
        airline=c.quote.airline,
        depart_time=c.quote.depart_time,
        return_time=c.quote.return_time,
        hotel_name=c.hotel.hotel_name if c.hotel else None,
    )

    if enricher is not None:
        # Candidates mode: price the hotel only now, after detection + dedup
        # (the expensive/limited resource is spent on real candidates only).
        enriched = await _enrich_with_hotel(deal, enricher, cfg, stats)
        if enriched is None:
            return  # hotel priced the trip out of budget
        deal = enriched

    if confirmer is not None:  # Layer 2, pay-per-use
        try:
            deal = await confirmer.confirm(deal)
        except Exception:
            # Live confirmation is an upgrade, not a gate: if Duffel is down
            # the signal alert must still go out (unconfirmed).
            stats.errors += 1
            log.exception("confirmer failed for %s->%s; alerting unconfirmed",
                          route.origin, route.destination)
        # After enrichment total_price is in trip units; compare like with like.
        cap = cfg.trip_budget_cap if enricher is not None else cfg.budget_cap
        if deal.total_price > cap:
            log.info("live price killed the deal: %s->%s %.0f EUR",
                     route.origin, route.destination, deal.total_price)
            return
        # El confirmador elige la oferta viva más barata, que puede salir a
        # una hora que el filtro de horario excluye — un "confirmado" no
        # puede anunciar justo el madrugón que el usuario apagó.
        if not _schedule_ok(deal.depart_time, deal.return_time,
                            cfg.depart_after, cfg.return_before):
            log.info("confirmed offer violates the schedule filter: %s->%s "
                     "ida %s vuelta %s", route.origin, route.destination,
                     deal.depart_time, deal.return_time)
            return

    # The deals table keeps DETECTION units (total consistent with baseline /
    # drop_pct); the enriched trip total lives in the alert itself.
    await store.record_deal(route.id, replace(deal, total_price=c.total))
    sent: list[str] = []
    for notifier in notifiers:
        try:
            await notifier.send(deal)
            sent.append(notifier.channel)
        except Exception:
            stats.errors += 1
            log.exception("notifier %s failed", notifier.channel)
    if sent:
        # The dedup ledger stores DETECTION totals (c.total), not enriched or
        # confirmed ones: should_alert compares against future detection
        # totals, and mixing units would break the re-alert rule.
        await store.mark_alerted(key, c.total, sent)
        stats.alerts_sent += 1


async def _enrich_with_hotel(
    deal: Deal, enricher: HotelSource, cfg: Settings, stats: TickStats
) -> Deal | None:
    """Add the cheapest live hotel to a fired deal; None = trip over budget.

    If the hotel source has no data (or errors), the flight-only alert still
    goes out — the flight signal is valuable on its own.
    """
    assert deal.return_date is not None
    try:
        hotel = await enricher.cheapest(deal.destination, deal.depart_date, deal.return_date)
    except Exception:
        stats.errors += 1
        log.exception("hotel enrichment failed for %s %s", deal.destination, deal.depart_date)
        return deal
    if hotel is None:
        return deal
    trip_total = deal.total_price + hotel.price_per_night * deal.nights
    if trip_total > cfg.trip_budget_cap:
        log.info(
            "hotel priced the trip out of budget: %s->%s %s flights %.0f + hotel "
            "%.0f/night x %d = %.0f EUR > %.0f",
            deal.origin, deal.destination, deal.depart_date, deal.total_price,
            hotel.price_per_night, deal.nights, trip_total, cfg.trip_budget_cap,
        )
        return None
    return replace(
        deal,
        total_price=trip_total,
        hotel_link=hotel.deep_link,
        hotel_price_night=hotel.price_per_night,
        hotel_name=hotel.hotel_name,
    )
