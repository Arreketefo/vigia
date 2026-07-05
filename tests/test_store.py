from datetime import date

from vigia.contracts import Deal
from vigia.engine import dedup_key
from vigia.store import PriceStore


async def _route_id(store: PriceStore, origin: str = "ALC", dest: str = "BUD") -> int:
    await store.upsert_route(origin, dest)
    routes = await store.enabled_routes()
    return next(r.id for r in routes if r.origin == origin and r.destination == dest)


def _deal(total: float, key: str, depart=date(2026, 8, 1), ret=date(2026, 8, 5)) -> Deal:
    return Deal(
        origin="ALC", destination="BUD", depart_date=depart, return_date=ret,
        nights=(ret - depart).days, total_price=total, baseline=800.0,
        drop_pct=0.3, confirmed=False, dedup_key=key,
        flight_link=None, hotel_link=None,
    )


async def test_upsert_route_idempotent(store: PriceStore):
    await store.upsert_route("ALC", "BUD")
    await store.upsert_route("alc", "bud")  # case-insensitive dedup
    assert len(await store.enabled_routes()) == 1


async def test_baseline_from_observations(store: PriceStore):
    rid = await _route_id(store)
    for price in (100.0, 110.0, 120.0, 130.0, 140.0):
        await store.record_observation(
            route_id=rid, depart_date=date(2026, 8, 10), return_date=date(2026, 8, 14),
            nights=4, flight_price=price, hotel_price_night=50.0,
            source="test", deep_link=None,
        )
    # totals = price*2 + 50*4 = 400..480, median 440
    median, mad, sample = await store.baseline(rid, "2026-08", pax=2, with_hotel=True)
    assert sample == 5
    assert median == 440.0
    assert mad == 20.0


async def test_baseline_modes_do_not_mix(store: PriceStore):
    """Flight-only and flight+hotel observations feed separate baselines."""
    rid = await _route_id(store)
    for price, hotel in ((100.0, 50.0), (200.0, None)):
        await store.record_observation(
            route_id=rid, depart_date=date(2026, 8, 10), return_date=date(2026, 8, 14),
            nights=4, flight_price=price, hotel_price_night=hotel,
            source="test", deep_link=None,
        )
    median_h, _, sample_h = await store.baseline(rid, "2026-08", pax=2, with_hotel=True)
    median_f, _, sample_f = await store.baseline(rid, "2026-08", pax=2, with_hotel=False)
    assert (median_h, sample_h) == (400.0, 1)  # 100*2 + 50*4
    assert (median_f, sample_f) == (400.0, 1)  # 200*2, hotel row excluded


async def test_baseline_empty_month(store: PriceStore):
    rid = await _route_id(store)
    median, mad, sample = await store.baseline(rid, "2026-12", pax=2, with_hotel=True)
    assert (median, mad, sample) == (None, None, 0)


async def test_alert_dedup_and_realert(store: PriceStore):
    rid = await _route_id(store)
    depart, ret = date(2026, 8, 1), date(2026, 8, 5)

    # Never alerted -> yes
    assert await store.should_alert(rid, depart, ret, 500.0, realert_drop=0.10)

    key = dedup_key("ALC", "BUD", depart, ret, 500.0)
    await store.record_deal(rid, _deal(500.0, key))
    await store.mark_alerted(key, 500.0, ["telegram"])

    # Same price again -> suppressed
    assert not await store.should_alert(rid, depart, ret, 500.0, realert_drop=0.10)
    # 5% better (different 25-EUR bucket, hence new dedup_key) -> still suppressed
    assert not await store.should_alert(rid, depart, ret, 475.0, realert_drop=0.10)
    # 10% better -> re-alert
    assert await store.should_alert(rid, depart, ret, 450.0, realert_drop=0.10)
    # Other dates unaffected
    assert await store.should_alert(rid, date(2026, 8, 8), date(2026, 8, 12), 500.0, 0.10)


async def test_stalest_pairs_prioritizes_unscanned(store: PriceStore):
    rid_bud = await _route_id(store, dest="BUD")
    await _route_id(store, dest="PRG")
    await store.mark_scanned(rid_bud, "2026-08")
    pairs = await store.stalest_route_month_pairs(["2026-08"], limit=2)
    # PRG/2026-08 was never scanned -> must come first
    assert pairs[0][0].destination == "PRG"
    assert pairs[1][0].destination == "BUD"


async def test_stalest_pairs_rotate_even_without_observations(store: PriceStore):
    """A scanned-but-empty pair must go to the back of the queue (no starvation)."""
    rid = await _route_id(store, dest="BUD")
    await _route_id(store, dest="PRG")
    await store.mark_scanned(rid, "2026-08")  # BUD scanned, recorded nothing
    pairs = await store.stalest_route_month_pairs(["2026-08"], limit=1)
    assert pairs[0][0].destination == "PRG"


async def test_prune_observations(store: PriceStore):
    rid = await _route_id(store)
    await store.record_observation(
        route_id=rid, depart_date=date(2026, 8, 10), return_date=date(2026, 8, 14),
        nights=4, flight_price=100.0, hotel_price_night=50.0,
        source="test", deep_link=None,
    )
    assert await store.prune_observations(45) == 0  # fresh rows survive
    await store._conn.execute(
        "UPDATE price_observations SET captured_at = datetime('now', '-60 days')"
    )
    assert await store.prune_observations(45) == 1  # aged rows are dropped


async def test_meta_roundtrip(store: PriceStore):
    assert await store.get_meta("last_tick_at") is None
    await store.set_meta("last_tick_at", "2026-07-05 10:00:00")
    assert await store.get_meta("last_tick_at") == "2026-07-05 10:00:00"
