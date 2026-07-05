"""End-to-end tick with fake sources: observation -> baseline -> deal -> alert -> dedup."""

from datetime import date

from conftest import make_settings

from vigia.contracts import Deal, FlightQuote, HotelQuote
from vigia.scheduler import month_buckets, tick
from vigia.store import PriceStore

DEPART, RETURN = date(2026, 9, 10), date(2026, 9, 14)


class FakeFlightSource:
    name = "fake-flights"

    def __init__(self, price: float, discovered: list[str] | None = None) -> None:
        self.price = price
        self.discovered = discovered or []
        self.calendar_calls: list[tuple[str, str, str]] = []

    async def search_range(self, origin, month_bucket, price_min, price_max):
        return [
            FlightQuote(
                origin=origin, destination=dest, depart_date=DEPART, return_date=RETURN,
                price=100.0, currency="eur", is_live=False, deep_link=None,
                source=self.name,
            )
            for dest in self.discovered
        ]

    async def calendar(self, origin, destination, month_bucket):
        self.calendar_calls.append((origin, destination, month_bucket))
        if month_bucket != DEPART.strftime("%Y-%m"):
            return []
        return [
            FlightQuote(
                origin=origin, destination=destination, depart_date=DEPART,
                return_date=RETURN, price=self.price, currency="eur",
                is_live=False, deep_link="https://example.test/flight", source=self.name,
            )
        ]


class FakeHotelSource:
    name = "fake-hotels"

    def __init__(self, price_per_night: float) -> None:
        self.price_per_night = price_per_night

    async def cheapest(self, location, check_in, check_out):
        return HotelQuote(
            location=location, check_in=check_in, check_out=check_out,
            price_per_night=self.price_per_night, currency="eur", is_live=False,
            deep_link="https://example.test/hotel", source=self.name,
        )


class CollectingNotifier:
    channel = "collect"

    def __init__(self) -> None:
        self.deals: list[Deal] = []

    async def send(self, deal: Deal) -> None:
        self.deals.append(deal)


def test_month_buckets_rolling_window():
    buckets = month_buckets(date(2026, 7, 5), window_days=330)
    assert buckets[0] == "2026-07"
    assert buckets[-1] == "2027-05"
    assert len(buckets) == 11


async def _seed_history(store: PriceStore, route_id: int, flight_price: float, n: int):
    for _ in range(n):
        await store.record_observation(
            route_id=route_id, depart_date=DEPART, return_date=RETURN, nights=4,
            flight_price=flight_price, hotel_price_night=100.0,
            source="test", deep_link=None,
        )


async def test_tick_fires_alert_and_dedups(store: PriceStore):
    cfg = make_settings(batch_size=50)
    await store.upsert_route("ALC", "BUD")
    route = (await store.enabled_routes())[0]
    # History: flight 300 * 2 pax + hotel 100 * 4 = 1000 EUR baseline
    await _seed_history(store, route.id, flight_price=300.0, n=10)

    # Cheap tick: flight 50 * 2 + hotel 100 * 4 = 500 -> 50% drop, under 600
    flights = FakeFlightSource(price=50.0)
    hotels = FakeHotelSource(price_per_night=100.0)
    notifier = CollectingNotifier()

    stats = await tick(flights, hotels, store, cfg, [notifier])
    assert stats.alerts_sent == 1
    assert len(notifier.deals) == 1
    deal = notifier.deals[0]
    assert deal.total_price == 500.0
    assert deal.nights == 4
    assert deal.baseline is not None and deal.baseline > 900
    assert not deal.confirmed
    assert deal.flight_link == "https://example.test/flight"
    # Sweep mode: hotel already inside total AND baseline -> no enrichment split
    assert deal.hotel_price_night is None

    # Same price next tick -> suppressed by dedup
    stats2 = await tick(flights, hotels, store, cfg, [notifier])
    assert stats2.alerts_sent == 0
    assert len(notifier.deals) == 1

    # 15% better -> re-alert
    flights_better = FakeFlightSource(price=12.0)  # total 424 < 500*0.9
    stats3 = await tick(flights_better, hotels, store, cfg, [notifier])
    assert stats3.alerts_sent == 1
    assert len(notifier.deals) == 2

    # Tick must have stamped the healthcheck key
    assert await store.get_meta("last_tick_at") is not None


async def test_tick_normal_price_no_alert(store: PriceStore):
    cfg = make_settings(batch_size=50)
    await store.upsert_route("ALC", "BUD")
    route = (await store.enabled_routes())[0]
    await _seed_history(store, route.id, flight_price=300.0, n=10)

    flights = FakeFlightSource(price=290.0)  # total 980 ~= baseline
    notifier = CollectingNotifier()
    stats = await tick(flights, FakeHotelSource(100.0), store, cfg, [notifier])
    assert stats.observations > 0
    assert stats.alerts_sent == 0
    assert notifier.deals == []


async def test_flight_only_mode_no_hotel_source(store: PriceStore):
    """hotels=None: totals are flight*pax and deals fire without hotel data."""
    cfg = make_settings(batch_size=50)
    await store.upsert_route("ALC", "BUD")
    route = (await store.enabled_routes())[0]
    # Flight-only history: 300 * 2 pax = 600 EUR baseline
    for _ in range(10):
        await store.record_observation(
            route_id=route.id, depart_date=DEPART, return_date=RETURN, nights=4,
            flight_price=300.0, hotel_price_night=None, source="test", deep_link=None,
        )

    flights = FakeFlightSource(price=100.0)  # total 200: -66% and hard steal
    notifier = CollectingNotifier()
    stats = await tick(flights, None, store, cfg, [notifier])
    assert stats.alerts_sent == 1
    deal = notifier.deals[0]
    assert deal.total_price == 200.0
    assert deal.hotel_link is None
    assert deal.baseline == 600.0


async def test_hotel_source_without_data_skips_quote(store: PriceStore):
    """A configured hotel source returning None must not create observations."""

    class EmptyHotelSource:
        name = "empty"

        async def cheapest(self, location, check_in, check_out):
            return None

    cfg = make_settings(batch_size=50)
    await store.upsert_route("ALC", "BUD")
    stats = await tick(
        FakeFlightSource(price=50.0), EmptyHotelSource(), store, cfg, [CollectingNotifier()]
    )
    assert stats.observations == 0
    assert stats.alerts_sent == 0


class FakeCities:
    """Country/name lookups without HTTP."""

    def __init__(self, countries: dict[str, str]) -> None:
        self._countries = countries

    async def name(self, code: str) -> str | None:
        return None

    async def country(self, code: str) -> str | None:
        return self._countries.get(code)


async def test_excluded_countries_filter_scan_and_discovery(store: PriceStore):
    cfg = make_settings(batch_size=50, discovery=True, exclude_countries="ES")
    await store.upsert_route("ALC", "IBZ")  # Spain: must not be scanned
    await store.upsert_route("ALC", "BUD")
    cities = FakeCities({"IBZ": "ES", "BUD": "HU", "PMI": "ES", "PRG": "CZ"})

    flights = FakeFlightSource(price=300.0, discovered=["PMI", "PRG"])
    await tick(flights, FakeHotelSource(100.0), store, cfg, [CollectingNotifier()],
               cities=cities)

    # Discovery skipped PMI (ES) but added PRG
    dests = {r.destination for r in await store.enabled_routes()}
    assert "PRG" in dests and "PMI" not in dests
    # No scanned pair may target Spain
    scanned = {call[1] for call in flights.calendar_calls}
    assert "IBZ" not in scanned
    assert scanned  # BUD/PRG did get scanned


async def test_discovery_adds_routes(store: PriceStore):
    cfg = make_settings(discovery=True, batch_size=0)
    flights = FakeFlightSource(price=100.0, discovered=["BUD", "PRG", "ALC"])
    stats = await tick(flights, FakeHotelSource(100.0), store, cfg, [CollectingNotifier()])
    dests = {r.destination for r in await store.enabled_routes()}
    assert dests == {"BUD", "PRG"}  # origin itself filtered out
    assert sorted(stats.discovered_routes) == ["BUD", "PRG"]


async def test_past_departures_are_ignored(store: PriceStore):
    """The Aviasales cache can still hold departed trips; they must not be
    recorded or alerted."""

    class StaleFlightSource(FakeFlightSource):
        async def calendar(self, origin, destination, month_bucket):
            return [
                FlightQuote(
                    origin=origin, destination=destination,
                    depart_date=date(2020, 1, 10), return_date=date(2020, 1, 14),
                    price=10.0, currency="eur", is_live=False, deep_link=None,
                    source=self.name,
                )
            ]

    cfg = make_settings(batch_size=50)
    await store.upsert_route("ALC", "BUD")
    notifier = CollectingNotifier()
    stats = await tick(StaleFlightSource(10.0), FakeHotelSource(100.0), store, cfg, [notifier])
    assert stats.observations == 0
    assert notifier.deals == []


async def test_empty_pairs_rotate_out_of_the_batch(store: PriceStore):
    """Regression: pairs yielding no data must not monopolize every batch."""
    cfg = make_settings(batch_size=1)
    await store.upsert_route("ALC", "BUD")
    flights = FakeFlightSource(price=300.0)  # only 2026-09 returns quotes
    await tick(flights, FakeHotelSource(100.0), store, cfg, [CollectingNotifier()])
    await tick(flights, FakeHotelSource(100.0), store, cfg, [CollectingNotifier()])
    scanned_buckets = [call[2] for call in flights.calendar_calls]
    # batch_size=1: two ticks must scan two DIFFERENT (route, month) pairs
    assert len(scanned_buckets) == 2
    assert scanned_buckets[0] != scanned_buckets[1]


class FakeEnricher:
    """HotelSource used in candidates mode."""

    name = "fake-enricher"

    def __init__(self, price_per_night: float | None) -> None:
        self.price_per_night = price_per_night
        self.calls = 0

    async def cheapest(self, location, check_in, check_out):
        self.calls += 1
        if self.price_per_night is None:
            return None
        return HotelQuote(
            location=location, check_in=check_in, check_out=check_out,
            price_per_night=self.price_per_night, currency="eur", is_live=True,
            deep_link="https://example.test/enriched-hotel", source=self.name,
        )


async def _flight_only_route_with_history(store: PriceStore, flight_price: float = 300.0):
    await store.upsert_route("ALC", "BUD")
    route = (await store.enabled_routes())[0]
    for _ in range(10):
        await store.record_observation(
            route_id=route.id, depart_date=DEPART, return_date=RETURN, nights=4,
            flight_price=flight_price, hotel_price_night=None, source="test", deep_link=None,
        )
    return route


async def test_candidates_mode_enriches_alert_with_hotel(store: PriceStore):
    cfg = make_settings(batch_size=50, budget_cap=300.0, trip_budget_cap=800.0)
    await _flight_only_route_with_history(store)  # flight-only baseline 600

    flights = FakeFlightSource(price=60.0)  # detection total 120 (-80%)
    enricher = FakeEnricher(price_per_night=100.0)
    notifier = CollectingNotifier()
    stats = await tick(flights, None, store, cfg, [notifier], enricher=enricher)

    assert stats.alerts_sent == 1
    deal = notifier.deals[0]
    assert deal.total_price == 120.0 + 100.0 * 4  # flights + hotel
    assert deal.hotel_price_night == 100.0
    assert deal.hotel_link == "https://example.test/enriched-hotel"
    assert deal.baseline == 600.0  # flight-only detection baseline
    assert enricher.calls == 1

    # The deals table keeps DETECTION units (consistent with baseline/drop)
    cur = await store._conn.execute("SELECT total_price FROM deals")
    assert [r["total_price"] for r in await cur.fetchall()] == [120.0]

    # Same prices next tick: dedup suppresses BEFORE enrichment (no API spend)
    stats2 = await tick(flights, None, store, cfg, [notifier], enricher=enricher)
    assert stats2.alerts_sent == 0
    assert enricher.calls == 1


async def test_candidates_mode_trip_budget_kills_deal(store: PriceStore):
    cfg = make_settings(batch_size=50, budget_cap=300.0, trip_budget_cap=400.0)
    await _flight_only_route_with_history(store)

    flights = FakeFlightSource(price=60.0)          # detection total 120, fires
    enricher = FakeEnricher(price_per_night=100.0)  # trip 520 > 400
    notifier = CollectingNotifier()
    stats = await tick(flights, None, store, cfg, [notifier], enricher=enricher)
    assert stats.alerts_sent == 0
    assert notifier.deals == []


async def test_candidates_mode_alerts_flight_only_when_no_hotel_data(store: PriceStore):
    cfg = make_settings(batch_size=50, budget_cap=300.0)
    await _flight_only_route_with_history(store)

    flights = FakeFlightSource(price=60.0)
    notifier = CollectingNotifier()
    stats = await tick(flights, None, store, cfg, [notifier],
                       enricher=FakeEnricher(price_per_night=None))
    assert stats.alerts_sent == 1
    deal = notifier.deals[0]
    assert deal.total_price == 120.0
    assert deal.hotel_price_night is None


async def test_small_sample_hard_steal_hides_baseline(store: PriceStore):
    """A hard-steal fired with < min_sample history must not show a noise
    baseline (the '-0%' / '+34%' confusion from the first real alerts)."""
    cfg = make_settings(batch_size=50)
    await store.upsert_route("ALC", "BUD")
    route = (await store.enabled_routes())[0]
    await _seed_history(store, route.id, flight_price=50.0, n=3)  # sample 3 < 8

    # total = 25*2 + 50*4 = 250 <= 600*0.6 -> fires via hard_steal only
    flights = FakeFlightSource(price=25.0)
    notifier = CollectingNotifier()
    stats = await tick(flights, FakeHotelSource(50.0), store, cfg, [notifier])
    assert stats.alerts_sent == 1
    deal = notifier.deals[0]
    assert deal.baseline is None
    assert deal.drop_pct is None


async def test_confirmer_failure_still_alerts_unconfirmed(store: PriceStore):
    """Layer 2 down must not silence the signal."""

    class BoomConfirmer:
        name = "boom"

        async def confirm(self, deal):
            raise RuntimeError("duffel down")

    cfg = make_settings(batch_size=50)
    await store.upsert_route("ALC", "BUD")
    route = (await store.enabled_routes())[0]
    await _seed_history(store, route.id, flight_price=300.0, n=10)

    notifier = CollectingNotifier()
    stats = await tick(FakeFlightSource(price=50.0), FakeHotelSource(100.0), store, cfg,
                       [notifier], confirmer=BoomConfirmer())
    assert stats.alerts_sent == 1
    assert not notifier.deals[0].confirmed
    assert stats.errors == 1


async def test_failing_notifier_does_not_mark_alerted(store: PriceStore):
    class BoomNotifier:
        channel = "boom"

        async def send(self, deal: Deal) -> None:
            raise RuntimeError("channel down")

    cfg = make_settings(batch_size=50)
    await store.upsert_route("ALC", "BUD")
    route = (await store.enabled_routes())[0]
    await _seed_history(store, route.id, flight_price=300.0, n=10)

    flights = FakeFlightSource(price=50.0)
    stats = await tick(flights, FakeHotelSource(100.0), store, cfg, [BoomNotifier()])
    assert stats.alerts_sent == 0
    assert stats.errors == 1

    # Channel recovers -> the same deal alerts on the next tick
    notifier = CollectingNotifier()
    stats2 = await tick(flights, FakeHotelSource(100.0), store, cfg, [notifier])
    assert stats2.alerts_sent == 1
    assert len(notifier.deals) == 1
