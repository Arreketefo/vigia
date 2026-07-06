import json
from datetime import date

import httpx

from vigia.contracts import Deal
from vigia.sources.duffel import DuffelPriceConfirmer


def _deal(**overrides):
    base = dict(
        origin="ALC", destination="BUD",
        depart_date=date(2026, 10, 9), return_date=date(2026, 10, 12), nights=3,
        total_price=520.0, baseline=600.0, drop_pct=0.8, confirmed=False,
        dedup_key="k", flight_link="https://f", hotel_link="https://h",
        hotel_price_night=100.0,  # flights part = 520 - 300 = 220
    )
    base.update(overrides)
    return Deal(**base)


def _confirmer(handler) -> DuffelPriceConfirmer:
    c = DuffelPriceConfirmer("duffel_test_x", pax=2)
    c._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers=c._client.headers
    )
    return c


def _offers_payload(*offers: tuple[str, str]) -> dict:
    return {
        "data": {
            "id": "orq_1",
            "offers": [
                {"id": f"off_{i}", "total_amount": amount, "total_currency": cur}
                for i, (amount, cur) in enumerate(offers)
            ],
        }
    }


async def test_confirm_repricess_flight_and_keeps_hotel():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        assert request.headers["Duffel-Version"] == "v2"
        assert request.headers["Authorization"].startswith("Bearer ")
        return httpx.Response(200, json=_offers_payload(("250.00", "EUR"), ("300.00", "EUR")))

    deal = await _confirmer(handler).confirm(_deal())
    assert deal.confirmed
    # New total = cheapest live flight total (250, all pax) + hotel 100*3
    assert deal.total_price == 550.0
    assert deal.hotel_price_night == 100.0
    # Drop recomputed against the LIVE flight price: (600-250)/600
    assert deal.drop_pct is not None and abs(deal.drop_pct - 350 / 600) < 1e-9
    # Round trip: two slices, pax passengers
    slices = captured["data"]["slices"]
    assert [s["origin"] for s in slices] == ["ALC", "BUD"]
    assert [s["destination"] for s in slices] == ["BUD", "ALC"]
    assert len(captured["data"]["passengers"]) == 2


async def test_confirm_no_offers_returns_unconfirmed():
    deal = await _confirmer(
        lambda r: httpx.Response(200, json=_offers_payload())
    ).confirm(_deal())
    assert not deal.confirmed
    assert deal.total_price == 520.0  # untouched


async def test_confirm_ignores_foreign_currency_offers():
    deal = await _confirmer(
        lambda r: httpx.Response(
            200, json=_offers_payload(("199.00", "GBP"), ("260.00", "EUR"))
        )
    ).confirm(_deal())
    assert deal.confirmed
    assert deal.total_price == 260.0 + 300.0


async def test_confirm_one_way_deal_passes_through():
    deal = _deal(return_date=None, nights=0, hotel_price_night=None)
    result = await _confirmer(lambda r: httpx.Response(500)).confirm(deal)
    assert result is deal  # no API call, unchanged


async def test_confirm_replaces_stale_flight_detail_with_live_offer():
    """El precio confirmado es de la oferta viva de Duffel: la aerolínea y
    los horarios de la quote cacheada no pueden acompañarlo."""
    payload = _offers_payload(("250.00", "EUR"))
    payload["data"]["offers"][0].update({
        "owner": {"iata_code": "VY", "name": "Vueling"},
        "slices": [
            {"segments": [{"departing_at": "2026-10-09T18:40:00"}]},
            {"segments": [{"departing_at": "2026-10-12T07:15:00"}]},
        ],
    })
    stale = _deal(airline="FR", depart_time="06:25", return_time="21:40")
    deal = await _confirmer(lambda r: httpx.Response(200, json=payload)).confirm(stale)
    assert deal.confirmed
    assert deal.airline == "VY"
    assert deal.depart_time == "18:40"
    assert deal.return_time == "07:15"


async def test_confirm_clears_flight_detail_when_offer_has_none():
    """Oferta viva sin slices/owner: mejor sin detalle que con uno falso."""
    stale = _deal(airline="FR", depart_time="06:25", return_time="21:40")
    deal = await _confirmer(
        lambda r: httpx.Response(200, json=_offers_payload(("250.00", "EUR")))
    ).confirm(stale)
    assert deal.confirmed
    assert deal.airline is None
    assert deal.depart_time is None
    assert deal.return_time is None
