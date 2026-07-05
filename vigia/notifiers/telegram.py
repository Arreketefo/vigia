import httpx

from vigia.cities import CityDirectory
from vigia.contracts import Deal
from vigia.notifiers.format import drop_display


def _escape_markdown(text: str) -> str:
    """Minimal escaping for legacy Markdown parse_mode (city names)."""
    for char in ("_", "*", "[", "`"):
        text = text.replace(char, f"\\{char}")
    return text


class TelegramNotifier:
    channel = "telegram"

    def __init__(
        self, bot_token: str, chat_id: str, cities: CityDirectory | None = None
    ) -> None:
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._cities = cities
        self._client = httpx.AsyncClient(timeout=10.0)

    async def send(self, deal: Deal) -> None:
        destination = deal.destination
        if self._cities is not None:
            name = await self._cities.name(deal.destination)
            if name:
                destination = f"{_escape_markdown(name)} ({deal.destination})"
        badge = "✅ LIVE" if deal.confirmed else "📡 señal"
        lines = [
            f"*{deal.origin} → {destination}* {badge}",
            f"{deal.depart_date} → {deal.return_date} ({deal.nights} noches)",
        ]
        if deal.baseline is not None and deal.drop_pct is not None:
            # With enrichment the baseline refers to flights only.
            label = "vuelos: típico" if deal.hotel_price_night is not None else "típico"
            lines.append(
                f"*Total: {deal.total_price:.0f} €* "
                f"({label} {deal.baseline:.0f} €, {drop_display(deal.drop_pct)})"
            )
        else:
            lines.append(f"*Total: {deal.total_price:.0f} €*")
        if deal.hotel_price_night is not None:
            flights_part = deal.total_price - deal.hotel_price_night * deal.nights
            # "por noche", not "/noche": Telegram renders /word as a bot command.
            lines.append(
                f"vuelos {flights_part:.0f} € + hotel {deal.hotel_price_night:.0f} € por noche"
            )
        links = [
            f"[{label}]({url})"
            for label, url in (("vuelo", deal.flight_link), ("hotel", deal.hotel_link))
            if url
        ]
        if links:
            lines.append(" · ".join(links))
        resp = await self._client.post(
            self._url,
            json={
                "chat_id": self._chat_id,
                "text": "\n".join(lines),
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
        )
        resp.raise_for_status()

    async def aclose(self) -> None:
        await self._client.aclose()
