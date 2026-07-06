"""LiteApiHotelSource tests — payload shapes taken from a real sandbox response
(2026-07-05): data[].roomTypes[].offerRetailRate.amount is the WHOLE-stay price.
El nombre del hotel llega por GET /data/hotel (el rates response solo trae ids),
salvo que el payload lo incluya inline."""

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


def with_name_lookup(post_handler, name: str = "Hotel Mock"):
    """Envuelve un handler de POST /hotels/rates atendiendo también el GET
    /data/hotel del lookup de nombre."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.path.endswith("/data/hotel")
            return httpx.Response(200, json={"data": {"name": name}})
        return post_handler(request)

    return handler


async def test_cheapest_normalizes_per_night_from_stay_total():
    def post_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["iataCode"] == "BUD"
        assert body["checkin"] == "2026-09-10"
        assert body["occupancies"] == [{"adults": 2}]
        assert body["maxRatesPerHotel"] == 1
        return httpx.Response(200, json=_rates_payload(798.89, 1431.01))

    src = LiteApiHotelSource("sand_test", adults=2)
    src._client = mock_client(with_name_lookup(post_handler))
    quote = await src.cheapest("BUD", date(2026, 9, 10), date(2026, 9, 14))
    assert quote is not None
    assert quote.price_per_night == 798.89 / 4
    assert quote.is_live
    assert quote.hotel_name == "Hotel Mock"
    # con nombre, el enlace aterriza en EL hotel (Google Hotels) CON las fechas
    assert quote.deep_link == (
        "https://www.google.com/travel/search"
        "?q=Hotel+Mock+BUD&checkin=2026-09-10&checkout=2026-09-14"
    )


async def test_deep_link_uses_city_name_when_directory_available():
    class FakeCities:
        async def name(self, code: str) -> str | None:
            return {"BUD": "Budapest"}.get(code.upper())

    src = LiteApiHotelSource("sand_test", city_names=FakeCities())  # type: ignore[arg-type]
    src._client = mock_client(with_name_lookup(
        lambda r: httpx.Response(200, json=_rates_payload(400.0))
    ))
    quote = await src.cheapest("BUD", date(2026, 9, 10), date(2026, 9, 12))
    assert quote is not None
    # "Budapest", no "BUD": el código IATA no desambigua nada en Google
    assert quote.deep_link and "q=Hotel+Mock+Budapest" in quote.deep_link


async def test_nameless_hotel_is_negative_cached():
    """Un 200 sin nombre es un hecho de la API, no un fallo: un solo GET."""
    gets = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            gets["n"] += 1
            return httpx.Response(200, json={"data": {"id": "lp00000"}})  # sin name
        return httpx.Response(200, json=_rates_payload(400.0))

    src = LiteApiHotelSource("sand_test")
    src._client = mock_client(handler)
    for _ in range(3):
        quote = await src.cheapest("BUD", date(2026, 9, 10), date(2026, 9, 12))
        assert quote is not None and quote.hotel_name is None
    assert gets["n"] == 1


async def test_inline_hotel_name_skips_the_lookup():
    payload = _rates_payload(400.0)
    payload["data"][0]["name"] = "Hotel Inline"
    gets = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            gets["n"] += 1
            return httpx.Response(200, json={"data": {"name": "no debería pedirse"}})
        return httpx.Response(200, json=payload)

    src = LiteApiHotelSource("sand_test")
    src._client = mock_client(handler)
    quote = await src.cheapest("BUD", date(2026, 9, 10), date(2026, 9, 12))
    assert quote is not None
    assert quote.hotel_name == "Hotel Inline"
    assert gets["n"] == 0


async def test_name_lookup_failure_does_not_kill_the_quote():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(500)
        return httpx.Response(200, json=_rates_payload(400.0))

    src = LiteApiHotelSource("sand_test")
    src._client = mock_client(handler)
    quote = await src.cheapest("BUD", date(2026, 9, 10), date(2026, 9, 12))
    assert quote is not None
    assert quote.hotel_name is None
    # sin nombre no hay ficha a la que apuntar: fallback al listado con fechas
    assert quote.deep_link and "booking.com" in quote.deep_link
    assert "checkin=2026-09-10" in quote.deep_link


async def test_quality_floor_hot_change_invalidates_quote_cache():
    """El suelo se ajusta en caliente: al cambiar, las quotes cacheadas se
    calcularon con el suelo viejo y deben invalidarse; si NO cambia, la
    caché sobrevive (el push del runtime llega en cada tick)."""
    bodies = []

    def post_handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_rates_payload(400.0))

    src = LiteApiHotelSource("sand_test", min_rating=7.0, min_reviews=50)
    src._client = mock_client(with_name_lookup(post_handler))
    await src.cheapest("BUD", date(2026, 9, 10), date(2026, 9, 12))
    assert len(bodies) == 1
    assert bodies[0]["minRating"] == 7.0
    assert bodies[0]["minReviewsCount"] == 50

    # mismo suelo re-empujado -> cache intacta, sin POST nuevo
    src.set_quality_floor(7.0, 50)
    await src.cheapest("BUD", date(2026, 9, 10), date(2026, 9, 12))
    assert len(bodies) == 1

    # suelo distinto -> cache invalidada, POST nuevo con el filtro nuevo
    src.set_quality_floor(8.5, 100)
    await src.cheapest("BUD", date(2026, 9, 10), date(2026, 9, 12))
    assert len(bodies) == 2
    assert bodies[1]["minRating"] == 8.5
    assert bodies[1]["minReviewsCount"] == 100

    # apagar el filtro también cuenta como cambio; sin suelo, las claves NI
    # aparecen en la petición
    src.set_quality_floor(0, 0)
    await src.cheapest("BUD", date(2026, 9, 10), date(2026, 9, 12))
    assert len(bodies) == 3
    assert "minRating" not in bodies[2]
    assert "minReviewsCount" not in bodies[2]


async def test_metro_code_falls_back_to_city_name():
    bodies = []

    def post_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "iataCode" in body:
            return httpx.Response(200, json={"data": []})  # LON: no airport match
        assert body["cityName"] == "London"
        assert body["countryCode"] == "GB"
        return httpx.Response(200, json=_rates_payload(400.0))

    src = LiteApiHotelSource("sand_test")
    src._client = mock_client(with_name_lookup(post_handler))
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
    posts = {"n": 0}

    def post_handler(request: httpx.Request) -> httpx.Response:
        posts["n"] += 1
        return httpx.Response(200, json=_rates_payload(400.0))

    src = LiteApiHotelSource("sand_test")
    src._client = mock_client(with_name_lookup(post_handler))
    for _ in range(5):
        await src.cheapest("BUD", date(2026, 9, 10), date(2026, 9, 12))
    assert posts["n"] == 1

    # Misses cached as well: no re-probe per tick for empty locations
    calls = {"n": 0}

    def empty_handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"data": []})

    src2 = LiteApiHotelSource("sand_test")
    src2._client = mock_client(empty_handler)
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
    src._client = mock_client(with_name_lookup(lambda r: httpx.Response(200, json=payload)))
    quote = await src.cheapest("BUD", date(2026, 9, 10), date(2026, 9, 12))
    assert quote is not None
    assert quote.price_per_night == 150.0  # HUF offer ignored, not min()'d as EUR


async def test_malformed_payload_returns_none():
    src = LiteApiHotelSource("sand_test")
    src._client = mock_client(lambda r: httpx.Response(200, json={"data": "??"}))
    assert await src.cheapest("BUD", date(2026, 9, 10), date(2026, 9, 12)) is None
