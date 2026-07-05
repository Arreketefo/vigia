"""LiteApiHotelSource tests — payload shapes taken from a real sandbox response
(2026-07-05): data[].roomTypes[].offerRetailRate.amount is the WHOLE-stay price."""

import json
from datetime import date

import httpx

from vigia.sources.liteapi import LiteApiHotelSource


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _rates_payload(*stay_totals: float) -> dict:
    return {
        "data": [
            {
                "hotelId": f"lp{i:05x}",
                "roomTypes": [
                    {
                        "offerId": "x",
                        "rates": [{"name": "Room"}],
                        "offerRetailRate": {"amount": total, "currency": "EUR"},
                    }
                ],
            }
            for i, total in enumerate(stay_totals)
        ]
    }


async def test_cheapest_normalizes_per_night_from_stay_total():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["iataCode"] == "BUD"
        assert body["checkin"] == "2026-09-10"
        assert body["occupancies"] == [{"adults": 2}]
        assert body["maxRatesPerHotel"] == 1
        return httpx.Response(200, json=_rates_payload(798.89, 1431.01))

    src = LiteApiHotelSource("sand_test", adults=2)
    src._client = mock_client(handler)
    quote = await src.cheapest("BUD", date(2026, 9, 10), date(2026, 9, 14))
    assert quote is not None
    assert quote.price_per_night == 798.89 / 4
    assert quote.is_live
    assert quote.deep_link and "checkin=2026-09-10" in quote.deep_link


async def test_metro_code_falls_back_to_city_name():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "iataCode" in body:
            return httpx.Response(200, json={"data": []})  # LON: no airport match
        assert body["cityName"] == "London"
        assert body["countryCode"] == "GB"
        return httpx.Response(200, json=_rates_payload(400.0))

    src = LiteApiHotelSource("sand_test")
    src._client = mock_client(handler)
    quote = await src.cheapest("LON", date(2026, 9, 10), date(2026, 9, 12))
    assert quote is not None
    assert quote.price_per_night == 200.0
    assert len(bodies) == 2

    # Second lookup reuses the resolved strategy: one request, not two
    await src.cheapest("LON", date(2026, 10, 1), date(2026, 10, 3))
    assert len(bodies) == 3
    assert "cityName" in bodies[2]


async def test_unknown_location_returns_none():
    src = LiteApiHotelSource("sand_test")
    src._client = mock_client(lambda r: httpx.Response(200, json={"data": []}))
    assert await src.cheapest("XXX", date(2026, 9, 10), date(2026, 9, 12)) is None


async def test_quote_cache_bounds_api_spend():
    """Same trip re-queried (e.g. budget-killed candidate re-firing every
    tick) must hit the TTL cache, not the API — misses are cached too."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_rates_payload(400.0))

    src = LiteApiHotelSource("sand_test")
    src._client = mock_client(handler)
    for _ in range(5):
        await src.cheapest("BUD", date(2026, 9, 10), date(2026, 9, 12))
    assert calls["n"] == 1

    # Misses cached as well: no re-probe per tick for empty locations
    def empty_handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"data": []})

    src2 = LiteApiHotelSource("sand_test")
    src2._client = mock_client(empty_handler)
    calls["n"] = 0
    for _ in range(3):
        assert await src2.cheapest("XXX", date(2026, 9, 10), date(2026, 9, 12)) is None
    assert calls["n"] == 1


async def test_foreign_currency_offers_are_skipped():
    payload = {
        "data": [
            {"hotelId": "a", "roomTypes": [
                {"offerRetailRate": {"amount": 79889.0, "currency": "HUF"}}]},
            {"hotelId": "b", "roomTypes": [
                {"offerRetailRate": {"amount": 300.0, "currency": "EUR"}}]},
        ]
    }
    src = LiteApiHotelSource("sand_test")
    src._client = mock_client(lambda r: httpx.Response(200, json=payload))
    quote = await src.cheapest("BUD", date(2026, 9, 10), date(2026, 9, 12))
    assert quote is not None
    assert quote.price_per_night == 150.0  # HUF offer ignored, not min()'d as EUR


async def test_malformed_payload_returns_none():
    src = LiteApiHotelSource("sand_test")
    src._client = mock_client(lambda r: httpx.Response(200, json={"data": "??"}))
    assert await src.cheapest("BUD", date(2026, 9, 10), date(2026, 9, 12)) is None
