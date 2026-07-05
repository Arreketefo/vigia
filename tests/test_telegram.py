import json
from datetime import date

import httpx

from vigia.contracts import Deal
from vigia.notifiers.telegram import TelegramNotifier


class FakeCities:
    async def name(self, code: str) -> str | None:
        return {"IBZ": "Ibiza", "PMI": "Palma de Mallorca"}.get(code)


def _deal(**overrides):
    base = dict(
        origin="ALC", destination="IBZ",
        depart_date=date(2026, 7, 7), return_date=date(2026, 7, 10), nights=3,
        total_price=454.0, baseline=None, drop_pct=None, confirmed=False,
        dedup_key="k", flight_link="https://f", hotel_link="https://h",
        hotel_price_night=132.0,
    )
    base.update(overrides)
    return Deal(**base)


def _capture_notifier(captured: dict) -> TelegramNotifier:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    notifier = TelegramNotifier("tok", "42", cities=FakeCities())  # type: ignore[arg-type]
    notifier._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return notifier


async def test_send_shows_city_name_and_no_command_link():
    captured: dict = {}
    await _capture_notifier(captured).send(_deal())
    text = captured["text"]
    assert "Ibiza (IBZ)" in text
    assert "por noche" in text
    assert "/noche" not in text  # Telegram renders /word as a bot command
    assert "(3 noches)" in text
    assert "vuelos 58 € + hotel 132 € por noche" in text
    # No baseline -> no misleading typical-price line
    assert "típico" not in text


async def test_send_with_flight_baseline_label():
    captured: dict = {}
    await _capture_notifier(captured).send(
        _deal(baseline=178.0, drop_pct=0.51, total_price=347.0, hotel_price_night=130.0,
              nights=2, destination="PMI")
    )
    text = captured["text"]
    assert "Palma de Mallorca (PMI)" in text
    assert "vuelos: típico 178 €" in text
    assert "-51%" in text


async def test_send_unknown_city_falls_back_to_code():
    captured: dict = {}
    await _capture_notifier(captured).send(_deal(destination="XXX"))
    assert "ALC → XXX" in captured["text"]
