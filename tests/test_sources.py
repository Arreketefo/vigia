"""Parsing tests for the API sources, using canned payloads (shapes verified
against the live Travelpayouts docs, 2026-07) via httpx.MockTransport."""

import json
from datetime import date

import httpx
import pytest

from vigia.http import CircuitOpenError
from vigia.sources.aviasales import AviasalesFlightSource
from vigia.sources.hotellook import HotellookHotelSource


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_aviasales_calendar_parses_grouped_prices():
    payload = {
        "success": True,
        "data": {
            "2026-09-10": {
                "origin": "ALC",
                "destination": "BUD",
                "origin_airport": "ALC",
                "destination_airport": "BUD",
                "price": 89,
                "airline": "W6",
                "flight_number": "2376",
                "departure_at": "2026-09-10T06:35:00+02:00",
                "return_at": "2026-09-14T13:30:00+02:00",
                "transfers": 0,
                "return_transfers": 0,
                "duration": 175,
                "link": "/search/ALC1009BUD14092?t=W6",
            },
            "2026-09-11": {"origin": "ALC", "destination": "BUD"},  # no price -> skipped
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/aviasales/v3/grouped_prices"
        assert request.url.params["departure_at"] == "2026-09"
        assert request.url.params["group_by"] == "departure_at"
        assert request.url.params["market"] == "es"
        return httpx.Response(200, json=payload)

    src = AviasalesFlightSource("tok")
    src._client = mock_client(handler)
    quotes = await src.calendar("ALC", "BUD", "2026-09")
    assert len(quotes) == 1
    q = quotes[0]
    assert (q.depart_date, q.return_date, q.price) == (
        date(2026, 9, 10), date(2026, 9, 14), 89.0,
    )
    assert not q.is_live
    assert q.deep_link == "https://www.aviasales.com/search/ALC1009BUD14092?t=W6"


async def test_aviasales_calendar_uses_key_as_depart_fallback():
    payload = {"data": {"2026-09-10": {"origin": "ALC", "destination": "BUD", "price": 50}}}
    src = AviasalesFlightSource("tok")
    src._client = mock_client(lambda r: httpx.Response(200, json=payload))
    quotes = await src.calendar("ALC", "BUD", "2026-09")
    assert quotes[0].depart_date == date(2026, 9, 10)
    assert quotes[0].return_date is None
    # No link in the entry -> constructed search URL
    assert quotes[0].deep_link == "https://www.aviasales.com/search/ALC1009BUD"


async def test_aviasales_search_range_parses_price_range_payload():
    payload = {
        "currency": "eur",
        "success": True,
        "data": [
            {
                "departure_at": "2026-10-02",
                "destination_code": "PRG",
                "destination_name": "Prague",
                "destination_airport": "PRG",
                "origin_code": "ALC",
                "origin_airport": "ALC",
                "price": 120,
                "transfers": 0,
                "duration": 90,
                "link": "/PRG0210ALC1?t=FR",
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/aviasales/v3/search_by_price_range"
        # 'destination=-' is rejected by current API validation; must be absent
        assert "destination" not in request.url.params
        assert request.url.params["one_way"] == "false"
        assert request.headers["X-Access-Token"] == "tok"
        return httpx.Response(200, json=payload)

    src = AviasalesFlightSource("tok")
    # Preserve the source's real headers (auth) while mocking the transport
    src._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers=src._client.headers
    )
    quotes = await src.search_range("ALC", "2026-10", 0, 600)
    assert quotes[0].destination == "PRG"
    assert quotes[0].origin == "ALC"
    # Relative link without /search prefix -> anchored under /search/
    assert quotes[0].deep_link == "https://www.aviasales.com/search/PRG0210ALC1?t=FR"


async def test_hotellook_cheapest_normalizes_per_night():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("lookup.json"):
            return httpx.Response(
                200,
                json={"results": {"locations": [{"id": "12153", "cityName": "Budapest"}]}},
            )
        assert request.url.params["locationId"] == "12153"
        return httpx.Response(
            200,
            json=[
                {"hotelName": "A", "priceFrom": 240.0, "stars": 3},
                {"hotelName": "B", "priceFrom": 320.0, "stars": 4},
                {"hotelName": "C", "priceFrom": 0},  # no price -> ignored
            ],
        )

    src = HotellookHotelSource("tok")
    src._client = mock_client(handler)
    quote = await src.cheapest("BUD", date(2026, 9, 10), date(2026, 9, 14))
    assert quote is not None
    # priceFrom is for the whole stay: 240 EUR / 4 nights
    assert quote.price_per_night == 60.0
    assert quote.deep_link and "locationId=12153" in quote.deep_link


async def test_hotellook_unknown_location_returns_none():
    src = HotellookHotelSource("tok")
    src._client = mock_client(
        lambda r: httpx.Response(200, json={"results": {"locations": []}})
    )
    assert await src.cheapest("XXX", date(2026, 9, 10), date(2026, 9, 14)) is None


async def test_hotellook_lookup_cached_across_calls():
    calls = {"lookup": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("lookup.json"):
            calls["lookup"] += 1
            return httpx.Response(200, json={"results": {"locations": [{"id": "1"}]}})
        return httpx.Response(200, json=[{"priceFrom": 100.0}])

    src = HotellookHotelSource("tok")
    src._client = mock_client(handler)
    await src.cheapest("BUD", date(2026, 9, 10), date(2026, 9, 14))
    await src.cheapest("BUD", date(2026, 9, 20), date(2026, 9, 24))
    assert calls["lookup"] == 1


async def test_circuit_breaker_opens_after_persistent_failures():
    src = AviasalesFlightSource("tok")
    src._client = mock_client(lambda r: httpx.Response(404))
    for _ in range(3):
        with pytest.raises(httpx.HTTPStatusError):
            await src.calendar("ALC", "BUD", "2026-09")
    # Breaker now open: fails fast without issuing the request
    with pytest.raises(CircuitOpenError):
        await src.calendar("ALC", "BUD", "2026-09")


async def test_aviasales_max_flight_hours_filters_by_duration():
    payload = {
        "data": {
            # duration is the ROUND-TRIP total in minutes
            "2026-09-10": {"origin": "ALC", "destination": "LON", "price": 80,
                           "return_at": "2026-09-14", "duration": 325},
            "2026-09-11": {"origin": "ALC", "destination": "LON", "price": 60,
                           "return_at": "2026-09-15", "duration": 700},  # > 2*4h
            "2026-09-12": {"origin": "ALC", "destination": "LON", "price": 70,
                           "return_at": "2026-09-16"},  # unknown duration -> passes
            # Per-leg fields win over the total: 410-min outbound > 4h even
            # though the round-trip total (480) squeaks under 2*4h
            "2026-09-13": {"origin": "ALC", "destination": "LON", "price": 50,
                           "return_at": "2026-09-17", "duration": 480,
                           "duration_to": 410, "duration_back": 70},
        }
    }
    src = AviasalesFlightSource("tok", max_flight_hours=4)
    src._client = mock_client(lambda r: httpx.Response(200, json=payload))
    quotes = await src.calendar("ALC", "LON", "2026-09")
    assert sorted(q.price for q in quotes) == [70.0, 80.0]


async def test_settings_blank_numeric_env_means_default(monkeypatch):
    from conftest import make_settings

    monkeypatch.setenv("VIGIA_MAX_FLIGHT_HOURS", "")
    monkeypatch.setenv("VIGIA_TRIP_BUDGET_CAP", "")
    cfg = make_settings()  # must not raise ValidationError
    assert cfg.max_flight_hours is None
    assert cfg.trip_budget_cap == 600.0


async def test_aviasales_malformed_payload_yields_empty():
    src = AviasalesFlightSource("tok")
    src._client = mock_client(
        lambda r: httpx.Response(200, content=json.dumps({"data": "??"}).encode())
    )
    assert await src.calendar("ALC", "BUD", "2026-09") == []
    assert await src.search_range("ALC", "2026-09", 0, 600) == []
